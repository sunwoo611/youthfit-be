"""선정 사유. 검증·템플릿은 순수 함수로, 캐시·동시 답변 흐름은 롤백 트랜잭션에서 확인한다."""

import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.ingest import codes, sigungu
from app.llm.client import LlmError, Usage
from app.models import QuestionSession
from app.schemas.candidates import (
    AppliedConditions,
    EmploymentStatus,
    IncomeBucket,
    LinkSource,
    PolicyCandidate,
    PolicyEligibility,
)
from app.schemas.sessions import CachedReason, ReasonSource, SessionCreateRequest
from app.services import recommendation_service as svc
from app.services.policy_status import Availability
from app.services.recommendation_service import LlmReason, LlmReasons, Target
from app.services.session_service import SessionNotFoundError, create_session
from tests.test_candidates import TODAY, _insert_policies
from tests.test_sessions import _seoul_wide

SESSIONS = QuestionSession.__table__
USAGE = Usage(model="fake", input_tokens=1, output_tokens=1, latency_ms=1)

CONDITIONS = AppliedConditions(
    age=25,
    sido_code="11",
    employment_status=EmploymentStatus.UNEMPLOYED,
    interests=[],
    from_query=[],
)


def _target(
    plcy_no: str,
    *,
    scope: str = "sido",
    sigungu_codes: tuple[str, ...] = ("11680",),
    row: dict[str, Any] | None = None,
    **text: str,
) -> Target:
    card = PolicyCandidate(
        plcy_no=plcy_no,
        name=f"정책 {plcy_no}",
        org_name="서울시",
        categories=["일자리"],
        availability=Availability.OPEN,
        apply_start=None,
        apply_end=None,
        biz_end=None,
        eligibility=PolicyEligibility(
            age_limited=True,
            min_age=19,
            max_age=34,
            region_scope=scope,
            sido_codes=sorted({code[:2] for code in sigungu_codes})
            if scope == "sigungu"
            else ["11"],
            sigungu_codes=list(sigungu_codes) if scope == "sigungu" else [],
            employment=[],
        ),
        official_url="https://apply.example.go.kr/policy",
        url_source=LinkSource.APPLY,
    )
    base = {
        "marriage_code": None,
        "school_codes": [],
        "special_codes": [],
        "earn_cond_code": None,
        "earn_max": None,
    }
    return Target(card=card, row=base | (row or {}), text=text)


class FakeLlm:
    def __init__(
        self,
        items: list[LlmReason] | None = None,
        *,
        error: bool = False,
        on_call: Callable[[], Any] | None = None,
    ) -> None:
        self.items = items or []
        self.error = error
        self.on_call = on_call
        self.prompts: list[str] = []

    async def parse(self, system: str, user: str, schema: type[BaseModel]) -> tuple[Any, Usage]:
        self.prompts.append(user)
        if self.on_call:
            await self.on_call()
        if self.error:
            raise LlmError("LLM 호출 시간 초과 (15초)")
        assert schema is LlmReasons
        return LlmReasons(items=self.items), USAGE


# --- 텍스트 정리 ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("자세한 내용은 https://youth.seoul.go.kr/a?b=1 에서 확인", "자세한 내용은 에서 확인"),
        ("www.youthcenter.go.kr 참고", "참고"),
        ("youthcenter.go.kr/detail 참고", "참고"),
        ("youthcenter.go.kr에서 신청하세요", "에서 신청하세요"),
        ("온통청년youthcenter.go.kr 참고", "온통청년 참고"),
        ("(https://www.worklife.kr)를 확인하세요", "를 확인하세요"),
        ("문의(jejecks@korea.kr)", "문의"),
        ("naver.me/abc 또는 bit.ly/3abc 참고", "또는 참고"),
        ("월  최대\n20만원, 1.5배 지원", "월 최대 20만원, 1.5배 지원"),
    ],
)
def test_clean_text_removes_urls_and_collapses_spaces(text: str, expected: str):
    assert svc.clean_text(text, 200) == expected


def test_clean_text_clips_with_ellipsis():
    assert svc.clean_text("가" * 10, 5) == "가가가가…"
    assert svc.clean_text(None, 5) == ""


# --- LLM 출력 검증 ---


def test_merge_drops_unknown_and_duplicate_ids_and_fills_missing_with_template():
    targets = [_target("P1"), _target("P2"), _target("P3")]
    items = [
        LlmReason(plcy_no="P1", reason="첫 사유", check_points=[]),
        LlmReason(plcy_no="P1", reason="중복 사유", check_points=[]),
        LlmReason(plcy_no="FAKE-9", reason="없는 정책", check_points=[]),
        LlmReason(plcy_no="P3", reason="https://evil.example.com", check_points=[]),
    ]

    reasons = svc.merge_reasons(CONDITIONS, targets, items)

    assert [r.plcy_no for r in reasons] == ["P1", "P2", "P3"]
    assert reasons[0].reason == "첫 사유"
    assert reasons[0].reason_source is ReasonSource.LLM
    # P2 는 빠졌고, P3 는 URL 을 지우면 빈 사유라 템플릿으로 채운다.
    assert [r.reason_source for r in reasons[1:]] == [ReasonSource.TEMPLATE] * 2


def test_merge_cleans_and_trims_reason_and_check_points():
    item = LlmReason(
        plcy_no="P1",
        reason="가" * 300 + " https://x.example.com",
        check_points=["소득 확인 www.bokjiro.go.kr", "", "제외 대상 확인", "넷째", "다섯째"],
    )

    (reason,) = svc.merge_reasons(CONDITIONS, [_target("P1")], [item])

    assert len(reason.reason) == svc.REASON_MAX_CHARS
    assert "http" not in reason.reason
    assert reason.check_points == ["소득 확인", "제외 대상 확인", "넷째"]


@pytest.mark.parametrize(
    "text", ["2026년 12월 31일까지 신청", "10월 1일부터", "마감이 임박", "2025.3 공고"]
)
def test_merge_replaces_reasons_and_drops_check_points_that_mention_dates(text: str):
    items = [
        LlmReason(plcy_no="P1", reason=text, check_points=[]),
        LlmReason(plcy_no="P2", reason="사유", check_points=[text, "소득 확인"]),
    ]

    first, second = svc.merge_reasons(CONDITIONS, [_target("P1"), _target("P2")], items)

    assert first.reason_source is ReasonSource.TEMPLATE
    assert second.reason_source is ReasonSource.LLM
    assert second.check_points == ["소득 확인"]


# --- 템플릿 사유 ---


def test_template_reason_uses_matched_conditions_and_db_text():
    target = _target(
        "P1",
        support_content="월 최대 20만원 지원",
        extra_qualification="서울 6개월 이상 거주",
        exclusion="국민취업지원제도 참여자",
    )

    reason = svc.template_reason(CONDITIONS, target)

    assert reason.reason_source is ReasonSource.TEMPLATE
    assert reason.reason == (
        "입력하신 조건으로 나이(만 19~34세) 자격에 맞는 정책이에요. 지원 내용: 월 최대 20만원 지원"
    )
    # 시도 범위는 시군구를 80% 이상 포함한다는 뜻이라 시군구를 모르면 지역을 확정하지 않는다.
    assert reason.check_points == [
        "거주 시군구가 지원 지역(서울특별시 대부분 시군구)에 해당하는지 확인해 주세요.",
        "추가 자격: 서울 6개월 이상 거주",
        "제외 대상: 국민취업지원제도 참여자",
    ]


def test_template_reason_asks_to_check_sigungu_when_unknown():
    reason = svc.template_reason(CONDITIONS, _target("P1", scope="sigungu"))

    assert "지역(" not in reason.reason
    assert reason.check_points[0] == (
        "거주 시군구가 지원 지역(서울특별시 일부 시군구(강남구))에 해당하는지 확인해 주세요."
    )


@pytest.mark.parametrize("scope", ["sido", "sigungu"])
def test_template_reason_states_region_only_when_sigungu_is_known(scope: str):
    conditions = CONDITIONS.model_copy(update={"sigungu_codes": sigungu.match_codes("11680")})

    reason = svc.template_reason(conditions, _target("P1", scope=scope))

    assert "지역(서울특별시 강남구)" in reason.reason
    assert not any("시군구" in point for point in reason.check_points)


def test_template_reason_names_district_cities_instead_of_codes():
    target = _target("P1", scope="sigungu", sigungu_codes=("48121", "48123", "48125"))

    reason = svc.template_reason(CONDITIONS, target)

    assert reason.check_points[0] == (
        "거주 시군구가 지원 지역(경상남도 일부 시군구(창원시))에 해당하는지 확인해 주세요."
    )


def test_template_reason_without_restrictions_is_still_a_sentence():
    conditions = AppliedConditions(
        age=None, sido_code=None, employment_status=None, interests=[], from_query=[]
    )
    for scope in ("sigungu", "national"):
        reason = svc.template_reason(conditions, _target("P1", scope=scope))
        assert reason.reason == "입력하신 조건으로 걸러지는 자격 제한이 없는 정책이에요."


@pytest.mark.parametrize(
    ("bucket", "earn_max", "certain"),
    [
        (IncomeBucket.UP_TO_5000, 3500, False),
        (IncomeBucket.UP_TO_5000, 5000, True),
        (IncomeBucket.OVER_7000, 9000, False),
        (None, 5000, False),
    ],
)
def test_template_reason_claims_income_only_when_whole_bucket_fits(
    bucket: IncomeBucket | None, earn_max: int, certain: bool
):
    conditions = CONDITIONS.model_copy(update={"income_bucket": bucket})
    target = _target("P1", row={"earn_cond_code": codes.EARN_RANGE, "earn_max": earn_max})

    reason = svc.template_reason(conditions, target)

    income = f"연소득 {earn_max:,}만원 이하"
    assert (f"소득({income})" in reason.reason) is certain
    assert (f"소득 기준({income})을 확인해 주세요." in reason.check_points) is not certain


def test_template_reason_never_claims_free_text_income_condition():
    conditions = CONDITIONS.model_copy(update={"income_bucket": IncomeBucket.UP_TO_3000})
    target = _target("P1", row={"earn_cond_code": "0043003"}, earn_etc="기준중위소득 150% 이하")

    reason = svc.template_reason(conditions, target)

    assert "소득(" not in reason.reason
    assert "소득 기준(기준중위소득 150% 이하)을 확인해 주세요." in reason.check_points


# --- 프롬프트 ---


def test_prompt_has_user_profile_and_policy_facts_but_no_links():
    target = _target("P1", support_content="상담 https://apply.example.go.kr/x 제공")

    prompt = svc.build_prompt(CONDITIONS, [target])

    assert '"나이": "만 25세"' in prompt
    assert '"취업 상태": "미취업자"' in prompt
    assert '"plcy_no": "P1"' in prompt
    assert "만 19~34세" in prompt
    assert "http" not in prompt
    assert "apply.example" not in prompt


# --- 캐시 ---


def test_cached_reasons_only_when_they_cover_current_targets():
    targets = [_target("P1"), _target("P2")]
    stored = [
        CachedReason(plcy_no=no, reason="사유", check_points=[], reason_source="llm").model_dump(
            mode="json"
        )
        for no in ("P2", "P1")
    ]

    assert [r.plcy_no for r in svc.cached_reasons(stored, targets)] == ["P1", "P2"]
    assert svc.cached_reasons(None, targets) is None
    assert svc.cached_reasons(stored[:1], targets) is None
    assert svc.cached_reasons([{"plcy_no": "P1"}], targets) is None


# --- LLM 호출 ---


async def test_write_reasons_falls_back_to_template_without_llm_or_on_error():
    targets = [_target("P1")]
    for llm in (None, FakeLlm(error=True)):
        reasons, from_llm = await svc.write_reasons(llm, CONDITIONS, targets)
        assert not from_llm
        assert reasons[0].reason_source is ReasonSource.TEMPLATE

    llm = FakeLlm([LlmReason(plcy_no="P1", reason="사유", check_points=[])])
    reasons, from_llm = await svc.write_reasons(llm, CONDITIONS, targets)
    assert from_llm
    assert reasons[0].reason_source is ReasonSource.LLM


async def test_write_reasons_treats_output_without_usable_reasons_as_failure():
    llm = FakeLlm([LlmReason(plcy_no="FAKE-1", reason="사유", check_points=[])])

    reasons, from_llm = await svc.write_reasons(llm, CONDITIONS, [_target("P1")])

    assert not from_llm
    assert reasons[0].reason_source is ReasonSource.TEMPLATE


# --- DB 통합 ---


def _connect(conn: AsyncConnection) -> svc.ConnectionFactory:
    @asynccontextmanager
    async def connect() -> AsyncIterator[AsyncConnection]:
        yield conn

    return connect


async def _session(conn: AsyncConnection) -> uuid.UUID:
    await _insert_policies(
        conn,
        *(
            _seoul_wide(f"P-{i}", support_content=f"지원 {i}", apply_url="https://a.go.kr")
            for i in range(7)
        ),
    )
    created = await create_session(conn, SessionCreateRequest(query="서울 25살 취준생"), TODAY)
    return created.session_id


async def _cache(conn: AsyncConnection, session_id: uuid.UUID) -> Any:
    return await conn.scalar(select(SESSIONS.c.recommendations).where(SESSIONS.c.id == session_id))


def _llm_for_all(prefix: str = "") -> FakeLlm:
    return FakeLlm(
        [LlmReason(plcy_no=f"P-{i}", reason=f"{prefix}사유 {i}", check_points=[]) for i in range(7)]
    )


async def test_recommend_returns_db_cards_with_llm_reasons_and_reuses_cache(
    db_conn: AsyncConnection,
):
    session_id = await _session(db_conn)
    llm = _llm_for_all()

    first = await svc.recommend(_connect(db_conn), llm, session_id)

    assert first.as_of == TODAY
    assert len(first.items) == svc.RECOMMEND_LIMIT
    item = first.items[0]
    assert item.reason_source is ReasonSource.LLM
    assert item.policy.official_url == "https://a.go.kr"
    assert item.reason == f"사유 {item.policy.plcy_no[2:]}"
    assert len(await _cache(db_conn, session_id)) == svc.RECOMMEND_LIMIT

    second = await svc.recommend(_connect(db_conn), llm, session_id)
    assert second == first
    assert len(llm.prompts) == 1


async def test_recommend_without_llm_uses_template_and_does_not_cache(db_conn: AsyncConnection):
    session_id = await _session(db_conn)

    result = await svc.recommend(_connect(db_conn), None, session_id)

    assert {item.reason_source for item in result.items} == {ReasonSource.TEMPLATE}
    assert await _cache(db_conn, session_id) is None


@pytest.mark.parametrize(
    "change",
    [
        {"answers_revision": SESSIONS.c.answers_revision + 1},
        {"expires_at": func.now()},
    ],
    ids=["answers-resubmitted", "expired"],
)
async def test_recommend_does_not_cache_when_session_changes_during_llm_call(
    db_conn: AsyncConnection, change: dict[str, Any]
):
    session_id = await _session(db_conn)

    async def change_session() -> None:
        await db_conn.execute(update(SESSIONS).where(SESSIONS.c.id == session_id).values(change))

    result = await svc.recommend(
        _connect(db_conn), FakeLlm(_llm_for_all().items, on_call=change_session), session_id
    )

    assert result.items[0].reason_source is ReasonSource.LLM
    assert await _cache(db_conn, session_id) is None


async def test_recommend_still_answers_when_cache_write_fails(db_conn: AsyncConnection):
    session_id = await _session(db_conn)
    calls = 0

    @asynccontextmanager
    async def connect() -> AsyncIterator[AsyncConnection]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise SQLAlchemyError("DB 연결 끊김")
        yield db_conn

    result = await svc.recommend(connect, _llm_for_all(), session_id)

    assert result.items[0].reason_source is ReasonSource.LLM
    assert calls == 2


async def test_recommend_regenerates_when_cache_is_stale_or_broken(db_conn: AsyncConnection):
    session_id = await _session(db_conn)
    await db_conn.execute(
        update(SESSIONS)
        .where(SESSIONS.c.id == session_id)
        .values(recommendations=[{"plcy_no": "P-0", "reason": 1}])
    )
    llm = _llm_for_all("새 ")

    result = await svc.recommend(_connect(db_conn), llm, session_id)

    assert result.items[0].reason.startswith("새 사유")
    assert len(llm.prompts) == 1


async def test_recommend_missing_session_raises_not_found(db_conn: AsyncConnection):
    with pytest.raises(SessionNotFoundError):
        await svc.recommend(_connect(db_conn), None, uuid.uuid4())
