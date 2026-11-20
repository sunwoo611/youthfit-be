"""1차 룰 필터. 사용자 조건으로 정책 후보를 SQL 로 좁힌다 (LLM 미사용).

신청 가능 여부는 `policy_status.policy_availability` 와 같은 규칙을 SQL 로 건다(ADR 004).
공식 링크는 DB 값에서 정해진 순서로 고른다(ADR 005).
"""

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from sqlalchemy import (
    ColumnElement,
    RowMapping,
    Select,
    and_,
    case,
    func,
    literal,
    or_,
    select,
)
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.orm import aliased

from app.ingest import codes
from app.models import Policy
from app.schemas.candidates import (
    AppliedConditions,
    CandidateRequest,
    CandidateResponse,
    EmploymentStatus,
    IncomeBucket,
    Interest,
    LinkSource,
    Marriage,
    PolicyCandidate,
    PolicyCandidates,
    PolicyEligibility,
    School,
)
from app.services.policy_status import Availability
from app.services.query_hints import extract_hints
from app.services.region_scope import (
    effective_national_expr,
    fake_national_expr,
    org_matches_sido_expr,
)

POLICY_LIMIT = 50
ONTONG_DETAIL_URL = "https://www.youthcenter.go.kr/youthPolicy/ythPlcyTotalSearch/ythPlcyDetail/{}"

EMPLOYMENT_JOB_CODE: dict[EmploymentStatus, str] = {
    EmploymentStatus.EMPLOYED: "0013001",
    EmploymentStatus.SELF_EMPLOYED: "0013002",
    EmploymentStatus.UNEMPLOYED: "0013003",
    EmploymentStatus.FREELANCER: "0013004",
    EmploymentStatus.DAY_LABORER: "0013005",
    EmploymentStatus.FOUNDER: "0013006",
    EmploymentStatus.SHORT_TERM: "0013007",
    EmploymentStatus.FARMER: "0013008",
}

MARRIAGE_CODE: dict[Marriage, str] = {
    Marriage.MARRIED: "0055001",
    Marriage.SINGLE: "0055002",
}

SCHOOL_CODE: dict[School, str] = {
    School.BELOW_HIGH_SCHOOL: "0049001",
    School.HIGH_SCHOOL_ENROLLED: "0049002",
    School.HIGH_SCHOOL_EXPECTED: "0049003",
    School.HIGH_SCHOOL_GRADUATED: "0049004",
    School.COLLEGE_ENROLLED: "0049005",
    School.COLLEGE_EXPECTED: "0049006",
    School.COLLEGE_GRADUATED: "0049007",
    School.GRADUATE_SCHOOL: "0049008",
}

# 답이 함께 해당하는 학력 코드. 졸업 예정자는 재학 중이고, 석·박사는 학사를 가진다.
# 재학 → 고졸, 대졸 → 고졸로는 넓히지 않는다. 재학생 제외(국민취업지원제도 등)와
# 고졸 전용 정책이 그 차이로 구분돼 있다 (2026-11-02 journal).
SCHOOL_MATCH: dict[School, tuple[str, ...]] = {
    **{school: (code,) for school, code in SCHOOL_CODE.items()},
    School.HIGH_SCHOOL_EXPECTED: (
        SCHOOL_CODE[School.HIGH_SCHOOL_EXPECTED],
        SCHOOL_CODE[School.HIGH_SCHOOL_ENROLLED],
    ),
    School.COLLEGE_EXPECTED: (
        SCHOOL_CODE[School.COLLEGE_EXPECTED],
        SCHOOL_CODE[School.COLLEGE_ENROLLED],
    ),
    School.GRADUATE_SCHOOL: (
        SCHOOL_CODE[School.GRADUATE_SCHOOL],
        SCHOOL_CODE[School.COLLEGE_GRADUATED],
    ),
}

# 구간 하한(만원, 초과). 소득 상한이 이 값 이하인 정책은 확실히 해당하지 않는다.
INCOME_BUCKET_FLOOR: dict[IncomeBucket, int] = {
    IncomeBucket.UP_TO_3000: 0,
    IncomeBucket.UP_TO_5000: 3000,
    IncomeBucket.UP_TO_7000: 5000,
    IncomeBucket.OVER_7000: 7000,
}
# earn_max 에는 원 단위·월소득 값이 섞여 있어 만원 단위 연소득으로 읽히는 범위만 쓴다.
EARN_MAX_TRUSTED_MIN, EARN_MAX_TRUSTED_MAX = 1000, 20000

# 대분류는 2025년(복지문화·교육·참여권리)과 2026년(금융･복지･문화 등) 체계가 섞여 있다.
# 구분점은 원문 그대로 반각 가운뎃점(U+FF65)이다.
_WELFARE = ("금융･복지･문화", "복지문화")
INTEREST_CATEGORIES: dict[Interest, tuple[tuple[str, ...], tuple[str, ...]]] = {
    Interest.HOUSING: (("주거",), ()),
    Interest.JOBS: (("일자리",), ()),
    Interest.FINANCE: (_WELFARE, ("취약계층 및 금융지원",)),
    Interest.EDUCATION: (("교육･직업훈련", "교육"), ()),
    Interest.WELFARE: (_WELFARE, ()),
    Interest.PARTICIPATION: (("참여･기반", "참여권리"), ()),
}

# 같은 사업이 띄어쓰기·괄호만 다르게 여러 번 등록된 경우를 한 건으로 본다(ADR 006).
_NAME_NOISE = r"[\s()\[\]<>「」『』·ㆍ･,.\-_'\"]+"

_STATUS_RANK = {
    Availability.OPEN: 0,
    Availability.UPCOMING: 1,
    Availability.UNVERIFIED: 2,
}

# 사용자의 시군구를 모르므로 일부 시군구 대상 정책은 자격이 불확실해 전국 정책 뒤에 둔다.
SIDO_WIDE_MIN_RATIO = 0.8
_REGION_SCOPES = ("sido", "national", "sigungu")
REGION_SIDO, REGION_NATIONAL, REGION_SIGUNGU = range(len(_REGION_SCOPES))


def availability_expr(today: date) -> ColumnElement[str]:
    """`policy_availability` 의 SQL 판. NULL 비교는 거짓이 되어 파이썬 쪽 None 처리와 같다."""
    fixed = Policy.apply_period_code == codes.APPLY_PERIOD_FIXED
    return case(
        (or_(Policy.apply_end < today, Policy.biz_end < today), Availability.CLOSED.value),
        (and_(fixed, Policy.apply_end.is_(None)), Availability.UNVERIFIED.value),
        (and_(fixed, Policy.apply_start > today), Availability.UPCOMING.value),
        (fixed, Availability.OPEN.value),
        (Policy.biz_start > today, Availability.UPCOMING.value),
        (Policy.apply_period_code == codes.APPLY_PERIOD_ALWAYS, Availability.OPEN.value),
        else_=Availability.UNVERIFIED.value,
    )


def policy_official_link(
    plcy_no: str, apply_url: str | None, ref_urls: Sequence[str] | None
) -> tuple[str, LinkSource]:
    if apply_url:
        return apply_url, LinkSource.APPLY
    first_ref = next((url for url in ref_urls or () if url), None)
    if first_ref:
        return first_ref, LinkSource.REFERENCE
    return ONTONG_DETAIL_URL.format(plcy_no), LinkSource.ONTONG_DETAIL


def resolve_conditions(request: CandidateRequest) -> AppliedConditions:
    """명시한 필드가 우선이고, 비어 있는 필드만 query 추출값으로 채운다."""
    hints = extract_hints(request.query)
    from_query: list[str] = []
    age, sido, employment = request.age, request.sido_code, request.employment_status
    interests = list(dict.fromkeys(request.interests))
    if age is None and hints.age is not None:
        age = hints.age
        from_query.append("age")
    if sido is None and hints.sido_code is not None:
        sido = hints.sido_code
        from_query.append("sido_code")
    if employment is None and hints.employment_status is not None:
        employment = hints.employment_status
        from_query.append("employment_status")
    if not interests and hints.interests:
        interests = list(hints.interests)
        from_query.append("interests")
    return AppliedConditions(
        age=age,
        sido_code=sido,
        employment_status=employment,
        interests=interests,
        from_query=from_query,
    )


def region_rank_expr(sido_code: str | None) -> ColumnElement[int]:
    """시도 전체 > 전국 > 일부 시군구.

    시도 전체 여부는 그 시도 시군구 코드를 SIDO_WIDE_MIN_RATIO 이상 포함하는지로 본다.
    시도의 시군구 수는 단일 시도 정책 중 최대 시군구 수로 구한다.
    기초지자체 + 전국 zip(가짜 전국)은 national 이 아니라 sigungu 로 둔다.
    """
    if sido_code is None:
        return case((effective_national_expr(), REGION_NATIONAL), else_=REGION_SIGUNGU)
    single_sido = aliased(Policy)
    sido_total = (
        select(func.max(func.cardinality(single_sido.sigungu_codes)))
        .where(single_sido.sido_codes == [sido_code])
        .scalar_subquery()
    )
    codes_in_sido = (
        func.unnest(Policy.sigungu_codes).table_valued("code").render_derived("codes_in_sido")
    )
    in_sido = (
        select(func.count())
        .select_from(codes_in_sido)
        .where(func.left(codes_in_sido.c.code, 2) == sido_code)
        .scalar_subquery()
    )
    return case(
        (effective_national_expr(), REGION_NATIONAL),
        (in_sido >= func.coalesce(sido_total, 0) * SIDO_WIDE_MIN_RATIO, REGION_SIDO),
        else_=REGION_SIGUNGU,
    )


def _policy_filters(
    conditions: AppliedConditions, region_rank: ColumnElement[int]
) -> list[ColumnElement[bool]]:
    """역질문 답(혼인·학력·소득·시군구)으로는 확실히 해당하지 않는 정책만 지운다."""
    filters: list[ColumnElement[bool]] = []
    if conditions.age is not None:
        age = conditions.age
        filters.append(
            or_(
                Policy.age_limited.is_(False),
                and_(
                    or_(Policy.min_age.is_(None), Policy.min_age <= age),
                    or_(Policy.max_age.is_(None), Policy.max_age >= age),
                ),
            )
        )
    if conditions.sido_code is not None:
        # 가짜 전국(전국 zip + 기초지자체)은 기관명에 해당 시도명이 있을 때만 통과.
        sido = conditions.sido_code
        filters.append(
            and_(
                Policy.sido_codes.contains([sido]),
                or_(
                    ~fake_national_expr(),
                    org_matches_sido_expr(sido),
                ),
            )
        )
    else:
        filters.append(effective_national_expr())
    if conditions.employment_status is not None:
        job = EMPLOYMENT_JOB_CODE[conditions.employment_status]
        filters.append(
            or_(
                func.cardinality(Policy.job_codes) == 0,
                Policy.job_codes.overlap([codes.JOB_ANY, job]),
            )
        )
    if conditions.interests:
        clauses = []
        for interest in conditions.interests:
            categories, subcategories = INTEREST_CATEGORIES[interest]
            clause = Policy.categories.overlap(list(categories))
            if subcategories:
                clause = and_(clause, Policy.subcategories.overlap(list(subcategories)))
            clauses.append(clause)
        filters.append(or_(*clauses))
    if conditions.sigungu_codes:
        # 시도 전체 정책에서 빠진 시군은 불참으로 본다(예: 경기도 청년기본소득의 성남·고양).
        # 전국 정책은 코드 누락이 데이터 오류일 수 있어 거르지 않는다 (2026-11-02 journal).
        filters.append(
            or_(
                region_rank == REGION_NATIONAL,
                Policy.sigungu_codes.overlap(list(conditions.sigungu_codes)),
            )
        )
    if conditions.marriage is not None:
        filters.append(
            or_(
                Policy.marriage_code.is_(None),
                Policy.marriage_code.in_([codes.MARRIAGE_ANY, MARRIAGE_CODE[conditions.marriage]]),
            )
        )
    if conditions.school is not None:
        # 학력 8개를 전부 나열한 정책은 사용자 코드와 겹치므로 따로 처리하지 않는다.
        filters.append(
            or_(
                func.cardinality(Policy.school_codes) == 0,
                Policy.school_codes.overlap(
                    [codes.SCHOOL_ANY, codes.SCHOOL_ETC, *SCHOOL_MATCH[conditions.school]]
                ),
            )
        )
    floor = INCOME_BUCKET_FLOOR[conditions.income_bucket] if conditions.income_bucket else 0
    if floor > 0:
        filters.append(
            or_(
                Policy.earn_cond_code.is_distinct_from(codes.EARN_RANGE),
                Policy.earn_max.is_(None),
                Policy.earn_max < EARN_MAX_TRUSTED_MIN,
                Policy.earn_max > EARN_MAX_TRUSTED_MAX,
                Policy.earn_max > floor,
            )
        )
    return filters


def policy_query(
    conditions: AppliedConditions, today: date, limit: int | None = POLICY_LIMIT
) -> Select:
    status = availability_expr(today)
    status_rank = case(
        *((status == s.value, rank) for s, rank in _STATUS_RANK.items()),
        else_=len(_STATUS_RANK),
    )
    region_rank = region_rank_expr(conditions.sido_code)
    # 시군구를 알면 남은 일부 시군구 정책은 내 시군구 대상이므로 시도 전체와 같이 정렬한다.
    region_sort = (
        case((region_rank == REGION_SIGUNGU, REGION_SIDO), else_=region_rank)
        if conditions.sigungu_codes
        else region_rank
    )
    deadline = func.least(Policy.apply_end, Policy.biz_end)
    name_key = func.regexp_replace(Policy.name, _NAME_NOISE, literal(""), "g")
    # 다른 시군이 같은 이름으로 운영하는 별개 사업(예: 칠곡군·울진군 청년 이사비)은 합치지 않는다.
    sigungu_key = case((region_rank == REGION_SIGUNGU, Policy.sigungu_codes), else_=None)

    ranked = (
        select(
            Policy.plcy_no,
            Policy.name,
            Policy.org_name,
            Policy.categories,
            Policy.apply_start,
            Policy.apply_end,
            Policy.biz_end,
            Policy.age_limited,
            Policy.min_age,
            Policy.max_age,
            Policy.sido_codes,
            Policy.sigungu_codes,
            Policy.job_codes,
            Policy.marriage_code,
            Policy.school_codes,
            Policy.special_codes,
            Policy.earn_cond_code,
            Policy.earn_min,
            Policy.earn_max,
            Policy.apply_url,
            Policy.ref_urls,
            status.label("availability"),
            status_rank.label("status_rank"),
            region_rank.label("region_rank"),
            region_sort.label("region_sort"),
            deadline.label("deadline"),
            func.row_number()
            .over(
                partition_by=(name_key, sigungu_key),
                order_by=(
                    status_rank,
                    region_sort,
                    deadline.asc().nulls_last(),
                    Policy.plcy_no,
                ),
            )
            .label("dup_rank"),
        )
        .where(status != Availability.CLOSED.value, *_policy_filters(conditions, region_rank))
        .subquery("ranked")
    )
    return (
        select(ranked, func.count().over().label("total"))
        .where(ranked.c.dup_rank == 1)
        .order_by(
            ranked.c.status_rank,
            ranked.c.region_sort,
            ranked.c.deadline.asc().nulls_last(),
            ranked.c.plcy_no,
        )
        .limit(limit)
    )


def _policy_candidate(row: Mapping[str, Any]) -> PolicyCandidate:
    url, source = policy_official_link(row["plcy_no"], row["apply_url"], row["ref_urls"])
    job_codes = row["job_codes"] or []
    scope = _REGION_SCOPES[row["region_rank"]]
    return PolicyCandidate(
        plcy_no=row["plcy_no"],
        name=row["name"],
        org_name=row["org_name"],
        categories=row["categories"] or [],
        availability=Availability(row["availability"]),
        apply_start=row["apply_start"],
        apply_end=row["apply_end"],
        biz_end=row["biz_end"],
        eligibility=PolicyEligibility(
            age_limited=row["age_limited"],
            min_age=row["min_age"],
            max_age=row["max_age"],
            region_scope=scope,
            sido_codes=row["sido_codes"] or [],
            sigungu_codes=(row["sigungu_codes"] or []) if scope == "sigungu" else [],
            employment=[]
            if codes.JOB_ANY in job_codes
            else [codes.JOB.get(code, code) for code in job_codes],
        ),
        official_url=url,
        url_source=source,
    )


async def find_candidates(
    conn: AsyncConnection, request: CandidateRequest, today: date
) -> CandidateResponse:
    return await search_candidates(conn, resolve_conditions(request), today)


async def search_candidates(
    conn: AsyncConnection, conditions: AppliedConditions, today: date
) -> CandidateResponse:
    rows = await fetch_policy_rows(conn, conditions, today)
    return build_response(conditions, today, rows)


async def fetch_policy_rows(
    conn: AsyncConnection,
    conditions: AppliedConditions,
    today: date,
    limit: int | None = POLICY_LIMIT,
) -> Sequence[RowMapping]:
    return (await conn.execute(policy_query(conditions, today, limit))).mappings().all()


def build_response(
    conditions: AppliedConditions,
    today: date,
    policy_rows: Sequence[Mapping[str, Any]],
) -> CandidateResponse:
    """정책 카드는 정렬된 행의 앞 POLICY_LIMIT 건만 만든다."""
    return CandidateResponse(
        as_of=today,
        conditions=conditions,
        policies=PolicyCandidates(
            total=policy_rows[0]["total"] if policy_rows else 0,
            items=[_policy_candidate(row) for row in policy_rows[:POLICY_LIMIT]],
        ),
    )
