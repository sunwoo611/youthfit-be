"""선정 사유. 세션의 최종 후보 상위 정책마다 LLM 이 사유를 쓰고, 실패하면 템플릿으로 대체한다.

정책 카드(링크·마감일·자격)는 항상 DB 값으로 만들고, LLM 은 사유와 확인할 조건만 쓴다.
LLM 출력의 plcy_no 는 입력과 대조하고, 사유에 섞인 URL 은 지운다.
LLM 을 기다리는 동안 DB 연결을 잡지 않도록 읽기와 쓰기를 짧은 트랜잭션 두 개로 나눈다.
"""

import json
import logging
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from pydantic import BaseModel, TypeAdapter, ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.database import ConnectionFactory
from app.ingest import codes, sigungu
from app.llm.client import LlmClient, LlmError
from app.models import Policy, QuestionSession
from app.schemas.candidates import AppliedConditions, IncomeBucket, Interest, PolicyCandidate
from app.schemas.sessions import (
    CachedReason,
    ReasonSource,
    Recommendation,
    RecommendationResponse,
)
from app.services.candidate_service import (
    EARN_MAX_TRUSTED_MAX,
    EARN_MAX_TRUSTED_MIN,
    EMPLOYMENT_JOB_CODE,
    MARRIAGE_CODE,
    POLICY_LIMIT,
    SCHOOL_CODE,
    build_response,
    fetch_policy_rows,
)
from app.services.question_engine import INCOME_LABEL
from app.services.session_service import load_session, session_conditions

logger = logging.getLogger(__name__)

RECOMMEND_LIMIT = 5
REASON_MAX_CHARS = 200
CHECK_POINT_MAX_CHARS = 100
MAX_CHECK_POINTS = 3
# 정책 원문은 길이 편차가 커서 프롬프트에 넣기 전에 자른다. 사유는 지원 내용을 가장 많이 쓰고,
# 추가 자격·제외 대상은 대부분 비어 있어 절감 효과가 작고, 뒷부분의 제외 조건이 잘리면 사용자가
# 확인할 조건을 놓치므로 기존 상한을 유지한다.
SUPPORT_MAX_CHARS = 300
DESCRIPTION_MAX_CHARS = 200
CONDITION_TEXT_MAX_CHARS = 400

SESSIONS = QuestionSession.__table__

INTEREST_LABEL: dict[Interest, str] = {
    Interest.HOUSING: "주거",
    Interest.JOBS: "일자리",
    Interest.FINANCE: "금융",
    Interest.EDUCATION: "교육",
    Interest.WELFARE: "복지·문화",
    Interest.PARTICIPATION: "참여",
}

# 한글은 정규식에서 단어 문자라 \b 로 경계를 잡지 못한다. 도메인은 영문·숫자로 한정하고
# 앞뒤에 붙은 한글 단어·조사·괄호는 남긴다. 이메일은 도메인보다 먼저 통째로 지운다.
_URL_TAIL = r"[^\s()\[\]{}<>가-힣]*"
_URL = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    rf"|https?://{_URL_TAIL}"
    r"|(?<![A-Za-z0-9.-])(?:[A-Za-z0-9-]+\.)+"
    r"(?:go\.kr|or\.kr|co\.kr|ac\.kr|re\.kr|kr|com|net|org|me|ly|io)(?![A-Za-z0-9])"
    rf"(?:/{_URL_TAIL})?",
    re.IGNORECASE,
)
_EMPTY_BRACKETS = re.compile(r"\(\s*\)|\[\s*\]")
_SPACES = re.compile(r"\s+")
# LLM 은 원문의 지난 신청 기간을 옮겨 쓸 수 있다. 날짜는 화면의 DB 값만 믿는다.
_DATE = re.compile(
    r"20\d{2}\s*(?:년|[.\-/]\s*\d{1,2})|\d{1,2}\s*월\s*\d{1,2}\s*일|마감|신청\s*기간|접수\s*기간"
)

# 구간 상한(만원, 이하). 정책 소득 상한이 이 값 이상이어야 소득 자격을 확정할 수 있다.
INCOME_BUCKET_CEILING: dict[IncomeBucket, int | None] = {
    IncomeBucket.UP_TO_3000: 3000,
    IncomeBucket.UP_TO_5000: 5000,
    IncomeBucket.UP_TO_7000: 7000,
    IncomeBucket.OVER_7000: None,
}
_DISTRICT_CITY = {
    district: city for city, districts in sigungu.DISTRICTS.items() for district in districts
}

SYSTEM_PROMPT = """\
청년 정책 안내문 작성자다. 사용자 조건과 정책 정보(JSON)로 정책마다 선정 사유를 쓴다.

규칙:
- 입력 JSON에 있는 사실만 쓴다. 금액·자격·혜택을 추측하거나 지어내지 않는다.
- 링크, URL, 신청 기간, 마감일은 쓰지 않는다. 화면에 따로 표시된다.
- plcy_no 는 입력 값 그대로, 입력된 정책마다 한 항목씩 쓴다.
- 서버가 확인한 자격은 나이, 취업 상태, 혼인, 학력뿐이다. 본인 연소득은 구간이라 정책 소득 \
기준 충족 여부를 모르고, 거주 시군구를 모르면 지역 충족도 모른다. 이런 조건은 맞는다고 쓰지 \
말고 check_points 로 안내한다.
- reason: 서버가 확인한 자격과 핵심 지원 내용을 해요체 2문장, 200자 이내로 쓴다.
- check_points: 아직 판정하지 못한 자격(추가 자격, 제외 대상, 소득, 거주 시군구 등)을 \
사용자가 직접 확인할 문장으로 최대 3개 쓴다. 없으면 빈 목록이다.
"""


class LlmReason(BaseModel):
    """LLM 출력 1건. 길이·개수 제한은 서버가 잘라서 맞추므로 스키마에 걸지 않는다."""

    plcy_no: str
    reason: str
    check_points: list[str]


class LlmReasons(BaseModel):
    items: list[LlmReason]


_CACHE = TypeAdapter(list[CachedReason])


@dataclass(frozen=True)
class Target:
    """사유를 쓸 정책 1건. `row` 는 `policy_query` 행, `text` 는 서술용 원문 컬럼."""

    card: PolicyCandidate
    row: Mapping[str, Any]
    text: Mapping[str, Any]


@dataclass(frozen=True)
class _Context:
    as_of: date
    revision: int
    conditions: AppliedConditions
    targets: list[Target]
    has_more: bool
    persist_cache: bool
    recommendations_raw: Any


async def recommend(
    connect: ConnectionFactory,
    llm: LlmClient | None,
    session_id: uuid.UUID,
    exclude_plcy_nos: Sequence[str] = (),
) -> RecommendationResponse:
    """없거나 만료된 세션이면 SessionNotFoundError.

    후보 상위 POLICY_LIMIT 안에서 exclude 를 뺀 뒤 최대 RECOMMEND_LIMIT 건과 has_more 를 준다.
    exclude 가 비어 있을 때만 사유 캐시를 저장·전체 재사용하고, 있으면 plcy_no 별로 부분 재사용만
    한다.
    """
    async with connect() as conn:
        context = await _read(conn, session_id, exclude_plcy_nos)

    reasons = await _resolve_reasons(connect, llm, session_id, context)

    return RecommendationResponse(
        session_id=session_id,
        as_of=context.as_of,
        items=[
            Recommendation(policy=target.card, **reason.model_dump(exclude={"plcy_no"}))
            for target, reason in zip(context.targets, reasons, strict=True)
        ],
        has_more=context.has_more,
    )


async def _resolve_reasons(
    connect: ConnectionFactory,
    llm: LlmClient | None,
    session_id: uuid.UUID,
    context: _Context,
) -> list[CachedReason]:
    if context.persist_cache:
        cached = cached_reasons(context.recommendations_raw, context.targets)
        if cached is not None:
            return cached
        reasons, from_llm = await write_reasons(llm, context.conditions, context.targets)
        if from_llm:
            await _try_save(connect, session_id, context.revision, reasons)
        return reasons

    by_no = cache_by_plcy_no(context.recommendations_raw)
    missing = [target for target in context.targets if target.card.plcy_no not in by_no]
    if missing:
        filled, _ = await write_reasons(llm, context.conditions, missing)
        by_no = {**by_no, **{reason.plcy_no: reason for reason in filled}}
    return [by_no[target.card.plcy_no] for target in context.targets]


async def _try_save(
    connect: ConnectionFactory,
    session_id: uuid.UUID,
    revision: int,
    reasons: Sequence[CachedReason],
) -> None:
    try:
        async with connect() as conn:
            saved = await _save(conn, session_id, revision, reasons)
    except (OSError, SQLAlchemyError) as exc:
        # 사유는 이미 만들었으므로 캐시만 포기하고 응답한다.
        logger.warning("세션 %s 선정 사유 캐시 저장 실패: %s", session_id, type(exc).__name__)
    else:
        if not saved:
            logger.info("세션 %s 답이 바뀌어 선정 사유를 캐시하지 않음", session_id)


async def _read(
    conn: AsyncConnection, session_id: uuid.UUID, exclude_plcy_nos: Sequence[str]
) -> _Context:
    session = await load_session(conn, session_id)
    conditions = session_conditions(session)
    as_of = session["as_of"]
    rows = await fetch_policy_rows(conn, conditions, as_of, limit=POLICY_LIMIT)
    excluded = set(exclude_plcy_nos)
    remaining = [row for row in rows if row["plcy_no"] not in excluded]
    selected = remaining[:RECOMMEND_LIMIT]
    cards = build_response(conditions, as_of, selected).policies.items
    texts = await _policy_texts(conn, [card.plcy_no for card in cards])
    targets = [
        Target(card=card, row=row, text=texts.get(card.plcy_no, {}))
        for card, row in zip(cards, selected, strict=True)
    ]
    return _Context(
        as_of=as_of,
        revision=session["answers_revision"],
        conditions=conditions,
        targets=targets,
        has_more=len(remaining) > RECOMMEND_LIMIT,
        persist_cache=not exclude_plcy_nos,
        recommendations_raw=session["recommendations"],
    )


async def _policy_texts(
    conn: AsyncConnection, plcy_nos: Sequence[str]
) -> dict[str, Mapping[str, Any]]:
    if not plcy_nos:
        return {}
    result = await conn.execute(
        select(
            Policy.plcy_no,
            Policy.description,
            Policy.support_content,
            Policy.extra_qualification,
            Policy.exclusion,
            Policy.earn_etc,
        ).where(Policy.plcy_no.in_(plcy_nos))
    )
    return {row["plcy_no"]: row for row in result.mappings()}


def cache_by_plcy_no(raw: Any) -> dict[str, CachedReason]:
    """캐시를 plcy_no → 사유로 푼다. 없거나 형식이 틀리면 빈 dict (부분 재사용용)."""
    if raw is None:
        return {}
    try:
        return {reason.plcy_no: reason for reason in _CACHE.validate_python(raw)}
    except ValidationError:
        logger.warning("선정 사유 캐시 형식이 맞지 않아 다시 만든다")
        return {}


def cached_reasons(raw: Any, targets: Sequence[Target]) -> list[CachedReason] | None:
    """캐시가 지금 상위 정책을 모두 덮을 때만 그 순서대로 돌려준다.

    형식이 낡았거나 정책 구성이 바뀌었으면 없는 것으로 보고 다시 만든다.
    exclude 없는 첫 장에서만 쓴다. exclude 가 있으면 cache_by_plcy_no 부분 재사용.
    """
    if raw is None:
        return None
    try:
        reasons = _CACHE.validate_python(raw)
    except ValidationError:
        logger.warning("선정 사유 캐시 형식이 맞지 않아 다시 만든다")
        return None
    by_no = {reason.plcy_no: reason for reason in reasons}
    if set(by_no) != {target.card.plcy_no for target in targets}:
        return None
    return [by_no[target.card.plcy_no] for target in targets]


async def _save(
    conn: AsyncConnection, session_id: uuid.UUID, revision: int, reasons: Sequence[CachedReason]
) -> bool:
    """읽은 뒤 답이 다시 제출됐거나 세션이 만료됐으면 쓰지 않는다."""
    result = await conn.execute(
        update(SESSIONS)
        .where(
            SESSIONS.c.id == session_id,
            SESSIONS.c.answers_revision == revision,
            SESSIONS.c.expires_at > func.now(),
        )
        .values(recommendations=[reason.model_dump(mode="json") for reason in reasons])
    )
    return result.rowcount == 1


async def write_reasons(
    llm: LlmClient | None, conditions: AppliedConditions, targets: Sequence[Target]
) -> tuple[list[CachedReason], bool]:
    """(정책 순서대로의 사유, LLM 호출 성공 여부). 실패·키 없음이면 모두 템플릿 사유."""
    if llm is None or not targets:
        return [template_reason(conditions, target) for target in targets], False
    try:
        output, _ = await llm.parse(SYSTEM_PROMPT, build_prompt(conditions, targets), LlmReasons)
    except LlmError as exc:
        logger.warning("선정 사유 LLM 실패, 템플릿으로 대체: %s", exc)
        return [template_reason(conditions, target) for target in targets], False
    reasons = merge_reasons(conditions, targets, output.items)
    # 채택된 LLM 사유가 하나도 없으면 실패와 같으므로 다음 요청에서 다시 시도한다.
    return reasons, any(reason.reason_source is ReasonSource.LLM for reason in reasons)


def merge_reasons(
    conditions: AppliedConditions, targets: Sequence[Target], items: Sequence[LlmReason]
) -> list[CachedReason]:
    """입력에 없는 plcy_no·중복은 버리고, 비었거나 날짜를 언급했거나 빠진 정책은 템플릿 사유로
    채운다. 날짜를 언급한 확인할 조건은 뺀다."""
    wanted = {target.card.plcy_no for target in targets}
    by_no: dict[str, LlmReason] = {}
    for item in items:
        if item.plcy_no in wanted and item.plcy_no not in by_no:
            by_no[item.plcy_no] = item
    dropped = len(items) - len(by_no)
    if dropped:
        logger.info("LLM 선정 사유 %d건을 입력에 없거나 중복이라 버림", dropped)

    reasons = []
    for target in targets:
        item = by_no.get(target.card.plcy_no)
        reason = clean_text(item.reason, REASON_MAX_CHARS) if item else ""
        if item is None or not reason or _DATE.search(reason):
            reasons.append(template_reason(conditions, target))
            continue
        check_points = [clean_text(point, CHECK_POINT_MAX_CHARS) for point in item.check_points]
        reasons.append(
            CachedReason(
                plcy_no=target.card.plcy_no,
                reason=reason,
                check_points=[p for p in check_points if p and not _DATE.search(p)][
                    :MAX_CHECK_POINTS
                ],
                reason_source=ReasonSource.LLM,
            )
        )
    return reasons


def clean_text(text: str | None, limit: int) -> str:
    """URL 을 지우고 공백을 하나로 모은 뒤 limit 글자로 자른다."""
    text = _SPACES.sub(" ", _EMPTY_BRACKETS.sub("", _URL.sub("", text or ""))).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# --- 조건·자격 문구 (프롬프트와 템플릿 공용) ---


def _user_profile(conditions: AppliedConditions) -> dict[str, str]:
    """사용자가 알려준 조건만 담는다. 빠진 항목은 모르는 조건이다."""
    profile: dict[str, str] = {}
    if conditions.age is not None:
        profile["나이"] = f"만 {conditions.age}세"
    if conditions.sido_code:
        region = codes.SIDO.get(conditions.sido_code, conditions.sido_code)
        names = sigungu.SIGUNGU.get(conditions.sido_code, {})
        picked = [names[code] for code in conditions.sigungu_codes if code in names]
        profile["거주지"] = " ".join([region, *picked])
    if conditions.employment_status is not None:
        job = EMPLOYMENT_JOB_CODE[conditions.employment_status]
        profile["취업 상태"] = codes.JOB[job]
    if conditions.interests:
        profile["관심 분야"] = ", ".join(INTEREST_LABEL[i] for i in conditions.interests)
    if conditions.marriage is not None:
        profile["혼인"] = codes.MARRIAGE[MARRIAGE_CODE[conditions.marriage]]
    if conditions.school is not None:
        profile["학력"] = codes.SCHOOL[SCHOOL_CODE[conditions.school]]
    if conditions.income_bucket is not None:
        profile["본인 연소득"] = INCOME_LABEL[conditions.income_bucket]
    return profile


def _age_text(card: PolicyCandidate) -> str | None:
    rule = card.eligibility
    if not rule.age_limited or (rule.min_age is None and rule.max_age is None):
        return None
    if rule.min_age is not None and rule.max_age is not None:
        return f"만 {rule.min_age}~{rule.max_age}세"
    if rule.min_age is not None:
        return f"만 {rule.min_age}세 이상"
    return f"만 {rule.max_age}세 이하"


def _sigungu_names(sigungu_codes: Sequence[str]) -> list[str]:
    """시군구 이름. 일반구는 상위 시 이름으로 묶고, 이름표에 없는 코드는 뺀다."""
    names: dict[str, None] = {}
    for code in sigungu_codes:
        table = sigungu.SIGUNGU.get(code[:2], {})
        name = table.get(code) or table.get(_DISTRICT_CITY.get(code, ""))
        if name:
            names[name] = None
    return list(names)


def _region_text(card: PolicyCandidate, sido_code: str | None) -> str:
    """'sido' 는 사용자 시도의 시군구를 대부분(SIDO_WIDE_MIN_RATIO 이상) 포함한다는 뜻이다."""
    rule = card.eligibility
    if rule.region_scope == "national":
        return "전국"
    if rule.region_scope == "sido" and sido_code:
        return f"{codes.SIDO.get(sido_code, sido_code)} 대부분 시군구"
    sidos = ", ".join(codes.SIDO.get(code, code) for code in rule.sido_codes)
    names = _sigungu_names(rule.sigungu_codes)
    if not names:
        return f"{sidos} 일부 지역"
    more = " 등" if len(names) > 5 else ""
    return f"{sidos} 일부 시군구({', '.join(names[:5])}{more})"


def _restricted(row_codes: Sequence[str] | None, table: Mapping[str, str], *free: str) -> list[str]:
    """'제한없음'·기타 코드가 있거나 비어 있으면 제한이 없다고 보고 빈 목록."""
    values = list(row_codes or [])
    if not values or any(code in free for code in values):
        return []
    return [table.get(code, code) for code in values]


def _income_text(target: Target) -> str | None:
    row = target.row
    if row["earn_cond_code"] == codes.EARN_RANGE:
        earn_max = row["earn_max"]
        if earn_max is not None and EARN_MAX_TRUSTED_MIN <= earn_max <= EARN_MAX_TRUSTED_MAX:
            return f"연소득 {earn_max:,}만원 이하"
        return "연소득 범위 조건 있음"
    if row["earn_cond_code"] == codes.EARN_ANY or row["earn_cond_code"] is None:
        return None
    return clean_text(target.text.get("earn_etc"), 60) or "소득 조건 있음"


def _income_certain(conditions: AppliedConditions, target: Target) -> bool:
    """사용자 소득 구간 전체가 정책 소득 상한 안에 들 때만 참. SQL 필터는 이것을 보장하지 않는다."""
    if conditions.income_bucket is None or target.row["earn_cond_code"] != codes.EARN_RANGE:
        return False
    ceiling = INCOME_BUCKET_CEILING[conditions.income_bucket]
    earn_max = target.row["earn_max"]
    return (
        ceiling is not None
        and earn_max is not None
        and EARN_MAX_TRUSTED_MIN <= earn_max <= EARN_MAX_TRUSTED_MAX
        and earn_max >= ceiling
    )


def _description(raw: str | None, raw_support: str | None) -> str:
    """정책 설명. 지원 내용에 통째로 들어 있으면 같은 말이 두 번 들어가므로 뺀다.

    자르기 전 원문끼리 비교한다. 설명이 더 길거나 앞부분만 같으면 새 정보가 있으므로 남긴다.
    """
    full = clean_text(raw, len(raw or "") + 1)
    if full and full in clean_text(raw_support, len(raw_support or "") + 1):
        return ""
    return clean_text(raw, DESCRIPTION_MAX_CHARS)


def _policy_facts(conditions: AppliedConditions, target: Target) -> dict[str, Any]:
    card, row, text = target.card, target.row, target.text
    marriage = row["marriage_code"]
    support = clean_text(text.get("support_content"), SUPPORT_MAX_CHARS)
    facts: dict[str, Any] = {
        "plcy_no": card.plcy_no,
        "정책명": card.name,
        "운영 기관": card.org_name,
        "분류": card.categories,
        "지원 내용": support,
        "정책 설명": _description(text.get("description"), text.get("support_content")),
        "나이": _age_text(card) or "제한 없음",
        "지역": _region_text(card, conditions.sido_code),
        "취업 상태": ", ".join(card.eligibility.employment) or "제한 없음",
        "혼인": codes.MARRIAGE.get(marriage, marriage)
        if marriage and marriage != codes.MARRIAGE_ANY
        else "제한 없음",
        "학력": ", ".join(
            _restricted(row["school_codes"], codes.SCHOOL, codes.SCHOOL_ANY, codes.SCHOOL_ETC)
        )
        or "제한 없음",
        "소득": _income_text(target) or "제한 없음",
        "특화 대상": ", ".join(_restricted(row["special_codes"], codes.SPECIAL, codes.SPECIAL_ANY)),
        "추가 자격": clean_text(text.get("extra_qualification"), CONDITION_TEXT_MAX_CHARS),
        "제외 대상": clean_text(text.get("exclusion"), CONDITION_TEXT_MAX_CHARS),
    }
    return {key: value for key, value in facts.items() if value not in ("", None, [])}


def build_prompt(conditions: AppliedConditions, targets: Sequence[Target]) -> str:
    payload = {
        "사용자 조건": _user_profile(conditions) or "알려준 조건 없음",
        "정책": [_policy_facts(conditions, target) for target in targets],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def template_reason(conditions: AppliedConditions, target: Target) -> CachedReason:
    """DB 값과 사용자 조건만으로 만든 사유. LLM 이 없거나 실패했을 때 쓴다.

    맞는 자격에는 SQL 필터가 보장하는 것만 넣고, 소득·모르는 시군구는 확인할 조건으로 돌린다.
    """
    card, row, text = target.card, target.row, target.text
    profile = _user_profile(conditions)
    matched: list[str] = []
    if "나이" in profile and (age := _age_text(card)):
        matched.append(f"나이({age})")
    scope = card.eligibility.region_scope
    region = _region_text(card, conditions.sido_code)
    region_known = (scope == "national" and conditions.sido_code) or conditions.sigungu_codes
    if region_known:
        matched.append(f"지역({region if scope == 'national' else profile['거주지']})")
    if "취업 상태" in profile and card.eligibility.employment:
        matched.append(f"취업 상태({profile['취업 상태']})")
    marriage = row["marriage_code"]
    if "혼인" in profile and marriage and marriage != codes.MARRIAGE_ANY:
        matched.append(f"혼인({profile['혼인']})")
    school = _restricted(row["school_codes"], codes.SCHOOL, codes.SCHOOL_ANY, codes.SCHOOL_ETC)
    if "학력" in profile and school:
        matched.append(f"학력({profile['학력']})")
    income = _income_text(target)
    income_certain = _income_certain(conditions, target)
    if income_certain:
        matched.append(f"소득({income})")

    reason = (
        f"입력하신 조건으로 {', '.join(matched)} 자격에 맞는 정책이에요."
        if matched
        else "입력하신 조건으로 걸러지는 자격 제한이 없는 정책이에요."
    )
    support = clean_text(text.get("support_content"), 100)
    if support:
        reason += f" 지원 내용: {support}"

    points: list[str] = []
    if scope != "national" and not conditions.sigungu_codes:
        points.append(f"거주 시군구가 지원 지역({region})에 해당하는지 확인해 주세요.")
    if income and not income_certain:
        points.append(f"소득 기준({income})을 확인해 주세요.")
    special = _restricted(row["special_codes"], codes.SPECIAL, codes.SPECIAL_ANY)
    if special:
        points.append(f"특화 대상({', '.join(special)}) 요건을 확인해 주세요.")
    for label, key in (("추가 자격", "extra_qualification"), ("제외 대상", "exclusion")):
        value = clean_text(text.get(key), CHECK_POINT_MAX_CHARS - len(label) - 2)
        if value:
            points.append(f"{label}: {value}")

    return CachedReason(
        plcy_no=card.plcy_no,
        reason=clean_text(reason, REASON_MAX_CHARS),
        check_points=points[:MAX_CHECK_POINTS],
        reason_source=ReasonSource.TEMPLATE,
    )
