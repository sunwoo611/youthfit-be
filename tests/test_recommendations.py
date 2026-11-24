"""선정 사유. 검증·템플릿은 순수 함수로, 캐시·동시 답변 흐름은 롤백 트랜잭션에서 확인한다."""

import json
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.api.deps import get_db_connector, get_llm_client
from app.api.rate_limit import RateLimit
from app.api.v1.sessions import REASON_LIMIT
from app.core.config import get_settings
from app.core.database import ConnectionFactory, dispose_engine
from app.ingest import codes, sigungu
from app.llm.client import LlmError, Usage
from app.main import app
from app.models import QuestionSession
from app.schemas.candidates import (
    AppliedConditions,
    EmploymentStatus,
    IncomeBucket,
    LinkSource,
    PolicyCandidate,
    PolicyEligibility,
)
from app.schemas.sessions import (
    Answer,
    AnswerRequest,
    CachedReason,
    QuestionVariable,
    ReasonSource,
    SessionCreateRequest,
)
from app.services import recommendation_service as svc
from app.services.policy_status import Availability
from app.services.recommendation_service import LlmReason, LlmReasons, Target
from app.services.session_service import SessionNotFoundError, answer_session, create_session
from tests.test_candidates import TODAY, _insert_policies
from tests.test_sessions import QUERY, _insert_mixed, _seoul_wide

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

    assert '"나이":"만 25세"' in prompt
    assert '"취업 상태":"미취업자"' in prompt
    assert '"plcy_no":"P1"' in prompt
    assert "만 19~34세" in prompt
    assert "http" not in prompt
    assert "apply.example" not in prompt


def _facts(target: Target) -> dict[str, Any]:
    return json.loads(svc.build_prompt(CONDITIONS, [target]))["정책"][0]


def test_prompt_policy_fact_keys_are_fixed():
    target = _target(
        "P1",
        support_content="월 20만원 지원",
        description="청년 월세 지원 사업",
        extra_qualification="무주택자",
        exclusion="기수급자",
    )

    assert list(_facts(target)) == [
        "plcy_no", "정책명", "운영 기관", "분류", "지원 내용", "정책 설명", "나이", "지역",
        "취업 상태", "혼인", "학력", "소득", "추가 자격", "제외 대상",
    ]  # fmt: skip


def test_prompt_omits_empty_fields_and_no_special_target_placeholder():
    facts = _facts(_target("P1"))

    assert "지원 내용" not in facts
    assert "정책 설명" not in facts
    assert "추가 자격" not in facts
    assert "특화 대상" not in facts


def test_prompt_truncates_long_texts_per_field():
    long = "가" * 1000
    facts = _facts(
        _target("P1", support_content=long, description="나" * 1000, extra_qualification=long)
    )

    assert len(facts["지원 내용"]) <= svc.SUPPORT_MAX_CHARS
    assert len(facts["정책 설명"]) <= svc.DESCRIPTION_MAX_CHARS
    assert len(facts["추가 자격"]) <= svc.CONDITION_TEXT_MAX_CHARS


def test_prompt_drops_description_repeating_support():
    same = _facts(_target("P1", support_content="월 20만원을 12개월 지원", description="월 20만원"))
    different = _facts(
        _target("P1", support_content="월 20만원 지원", description="무주택 청년 대상")
    )

    assert "정책 설명" not in same
    assert different["정책 설명"] == "무주택 청년 대상"


def test_prompt_is_compact_json():
    prompt = svc.build_prompt(CONDITIONS, [_target("P1")])

    assert '": ' not in prompt
    assert '", "' not in prompt


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


def _connect(conn: AsyncConnection) -> ConnectionFactory:
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
    created = await create_session(
        _connect(conn), None, SessionCreateRequest(query="서울 25살 취준생"), TODAY
    )
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


@pytest.mark.parametrize(
    "make_llm", [lambda: None, lambda: FakeLlm(error=True)], ids=["no-key", "llm-error"]
)
async def test_recommend_without_usable_llm_uses_template_and_does_not_cache(
    db_conn: AsyncConnection, make_llm: Callable[[], FakeLlm | None]
):
    session_id = await _session(db_conn)

    result = await svc.recommend(_connect(db_conn), make_llm(), session_id)

    assert len(result.items) == svc.RECOMMEND_LIMIT
    assert {item.reason_source for item in result.items} == {ReasonSource.TEMPLATE}
    assert await _cache(db_conn, session_id) is None


class EchoLlm(FakeLlm):
    """프롬프트에 들어온 정책마다 `prefix` 를 붙인 사유를 돌려준다."""

    def __init__(self) -> None:
        super().__init__()
        self.prefix = ""

    async def parse(self, system: str, user: str, schema: type[BaseModel]) -> tuple[Any, Usage]:
        self.items = [
            LlmReason(plcy_no=policy["plcy_no"], reason=f"{self.prefix}사유", check_points=[])
            for policy in json.loads(user)["정책"]
        ]
        return await super().parse(system, user, schema)


async def test_answer_resubmission_makes_recommend_write_new_reasons(db_conn: AsyncConnection):
    await _insert_mixed(db_conn)
    created = await create_session(
        _connect(db_conn), None, SessionCreateRequest(query=QUERY), TODAY
    )
    session_id = created.session_id
    (marriage,) = [q for q in created.questions if q.variable is QuestionVariable.MARRIAGE]
    llm = EchoLlm()
    llm.prefix = "첫 "
    first = await svc.recommend(_connect(db_conn), llm, session_id)
    assert {item.reason for item in first.items} == {"첫 사유"}

    answer = Answer(question_id=marriage.id, values=["married"])
    await answer_session(db_conn, session_id, AnswerRequest(answers=[answer]))
    llm.prefix = "새 "
    second = await svc.recommend(_connect(db_conn), llm, session_id)

    assert len(llm.prompts) == 2
    profiles = [json.loads(prompt)["사용자 조건"] for prompt in llm.prompts]
    assert "혼인" not in profiles[0]
    assert profiles[1]["혼인"] == "기혼"
    assert {item.reason for item in second.items} == {"새 사유"}
    assert {reason["reason"] for reason in await _cache(db_conn, session_id)} == {"새 사유"}

    assert await svc.recommend(_connect(db_conn), llm, session_id) == second
    assert len(llm.prompts) == 2


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


async def test_recommend_exclude_returns_next_page_without_overwriting_cache(
    db_conn: AsyncConnection,
):
    """상위 5를 exclude 하면 다른 후보가 오고, 첫 장 캐시는 그대로다."""
    # plcy_no 는 정렬 키라 두 자리로 맞춰 문자열 정렬이 숫자 순과 같게 한다.
    await _insert_policies(
        db_conn,
        *(
            _seoul_wide(f"P-{i:02d}", support_content=f"지원 {i}", apply_url="https://a.go.kr")
            for i in range(12)
        ),
    )
    created = await create_session(
        _connect(db_conn), None, SessionCreateRequest(query="서울 25살 취준생"), TODAY
    )
    session_id = created.session_id
    llm = FakeLlm(
        [LlmReason(plcy_no=f"P-{i:02d}", reason=f"사유 {i}", check_points=[]) for i in range(12)]
    )

    first = await svc.recommend(_connect(db_conn), llm, session_id)
    assert first.has_more is True
    assert len(first.items) == svc.RECOMMEND_LIMIT
    first_nos = [item.policy.plcy_no for item in first.items]
    assert first_nos == [f"P-{i:02d}" for i in range(5)]
    cached = await _cache(db_conn, session_id)
    assert [row["plcy_no"] for row in cached] == first_nos
    prompts_after_first = len(llm.prompts)

    second = await svc.recommend(_connect(db_conn), llm, session_id, first_nos)
    assert [item.policy.plcy_no for item in second.items] == [f"P-{i:02d}" for i in range(5, 10)]
    assert second.has_more is True
    assert set(item.policy.plcy_no for item in second.items).isdisjoint(first_nos)
    assert await _cache(db_conn, session_id) == cached
    assert len(llm.prompts) == prompts_after_first + 1

    # body 없이 다시 호출하면 첫 장·캐시 그대로다.
    again = await svc.recommend(_connect(db_conn), llm, session_id)
    assert again == first
    assert len(llm.prompts) == prompts_after_first + 1


async def test_recommend_exclude_all_returns_empty(db_conn: AsyncConnection):
    session_id = await _session(db_conn)
    llm = _llm_for_all()
    first = await svc.recommend(_connect(db_conn), llm, session_id)
    all_nos = [f"P-{i}" for i in range(7)]

    empty = await svc.recommend(_connect(db_conn), llm, session_id, all_nos)

    assert empty.items == []
    assert empty.has_more is False
    assert await _cache(db_conn, session_id) is not None
    assert [item.policy.plcy_no for item in first.items] == [
        row["plcy_no"] for row in await _cache(db_conn, session_id)
    ]


# --- API ---


@pytest.fixture
async def client(db_conn: AsyncConnection) -> AsyncIterator[AsyncClient]:
    app.dependency_overrides[get_db_connector] = lambda: _connect(db_conn)
    app.dependency_overrides[REASON_LIMIT] = lambda: None
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.clear()


def _recommend_url(session_id: uuid.UUID | str) -> str:
    return f"/v1/sessions/{session_id}/recommendations"


async def test_api_returns_db_cards_with_reasons_and_reuses_cache(
    client: AsyncClient, db_conn: AsyncConnection
):
    session_id = await _session(db_conn)
    llm = _llm_for_all()
    app.dependency_overrides[get_llm_client] = lambda: llm

    res = await client.post(_recommend_url(session_id))

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["session_id"] == str(session_id)
    assert body["as_of"] == TODAY.isoformat()
    assert len(body["items"]) == svc.RECOMMEND_LIMIT
    assert body["has_more"] is True
    item = body["items"][0]
    assert item["reason_source"] == "llm"
    assert item["policy"]["official_url"] == "https://a.go.kr"
    assert item["reason"] == f"사유 {item['policy']['plcy_no'][2:]}"

    again = await client.post(_recommend_url(session_id))
    assert again.json() == body
    assert len(llm.prompts) == 1


async def test_api_exclude_returns_next_page(client: AsyncClient, db_conn: AsyncConnection):
    session_id = await _session(db_conn)
    llm = _llm_for_all()
    app.dependency_overrides[get_llm_client] = lambda: llm

    first = await client.post(_recommend_url(session_id))
    assert first.status_code == 200, first.text
    first_nos = [item["policy"]["plcy_no"] for item in first.json()["items"]]

    second = await client.post(_recommend_url(session_id), json={"exclude_plcy_nos": first_nos})
    assert second.status_code == 200, second.text
    body = second.json()
    assert [item["policy"]["plcy_no"] for item in body["items"]] == ["P-5", "P-6"]
    assert body["has_more"] is False


async def test_api_exclude_over_limit_is_422(client: AsyncClient, db_conn: AsyncConnection):
    session_id = await _session(db_conn)

    res = await client.post(
        _recommend_url(session_id),
        json={"exclude_plcy_nos": [f"P-{i}" for i in range(46)]},
    )

    assert res.status_code == 422


async def test_api_without_llm_key_answers_with_template(
    client: AsyncClient, db_conn: AsyncConnection
):
    session_id = await _session(db_conn)

    res = await client.post(_recommend_url(session_id))

    assert res.status_code == 200, res.text
    assert {item["reason_source"] for item in res.json()["items"]} == {"template"}


async def test_api_missing_or_expired_session_is_404(client: AsyncClient, db_conn: AsyncConnection):
    assert (await client.post(_recommend_url(uuid.uuid4()))).status_code == 404

    session_id = await _session(db_conn)
    await db_conn.execute(
        update(SESSIONS).where(SESSIONS.c.id == session_id).values(expires_at=func.now())
    )
    assert (await client.post(_recommend_url(session_id))).status_code == 404


async def test_api_read_failure_is_503_and_logged(caplog: pytest.LogCaptureFixture):
    @asynccontextmanager
    async def broken() -> AsyncIterator[AsyncConnection]:
        raise OSError("connection refused")
        yield

    app.dependency_overrides[get_db_connector] = lambda: broken
    app.dependency_overrides[REASON_LIMIT] = lambda: None
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            res = await c.post(_recommend_url(uuid.uuid4()))
    finally:
        app.dependency_overrides.clear()

    assert res.status_code == 503
    assert any(
        record.exc_info for record in caplog.records if "선정 사유 조회 실패" in record.message
    )


async def test_db_connector_commits_when_block_ends():
    """서비스는 읽기와 캐시 쓰기를 따로 연 연결에서 하므로 블록이 끝날 때 커밋돼야 캐시가 남는다."""
    if not get_settings().database_url:
        pytest.skip("DATABASE_URL 미설정")
    table = f"connector_probe_{uuid.uuid4().hex[:12]}"
    connect = get_db_connector()
    try:
        try:
            async with connect() as conn:
                await conn.execute(text(f"CREATE TABLE {table} (id int)"))
        except OSError as exc:
            pytest.skip(f"DB 연결 불가: {type(exc).__name__}")
        async with connect() as conn:
            committed = await conn.scalar(text(f"SELECT to_regclass('public.{table}')"))
            await conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
    finally:
        await dispose_engine()

    assert committed is not None


async def test_api_is_rate_limited(client: AsyncClient, db_conn: AsyncConnection):
    session_id = await _session(db_conn)
    app.dependency_overrides[REASON_LIMIT] = RateLimit(per_ip=1, total=10, window_sec=60)

    assert (await client.post(_recommend_url(session_id))).status_code == 200
    res = await client.post(_recommend_url(session_id))
    assert res.status_code == 429
    assert "Retry-After" in res.headers
