"""1차 룰 필터. 사용자 조건으로 정책·적금 후보를 SQL 로 좁힌다 (LLM 미사용).

신청 가능 여부는 `policy_status.policy_availability` 와 같은 규칙을 SQL 로 건다(ADR 004).
공식 링크는 DB 값에서 정해진 순서로 고른다(ADR 005).
"""

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ColumnElement,
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
from app.models import FinancialCompany, Policy, SavingProduct, SavingProductOption
from app.schemas.candidates import (
    AppliedConditions,
    CandidateRequest,
    CandidateResponse,
    EmploymentStatus,
    Interest,
    LinkSource,
    PolicyCandidate,
    PolicyCandidates,
    PolicyEligibility,
    SavingCandidate,
    SavingCandidates,
)
from app.services.policy_status import Availability
from app.services.query_hints import extract_hints

POLICY_LIMIT = 50
SAVING_LIMIT = 20
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


def _national() -> ColumnElement[bool]:
    return func.cardinality(Policy.sigungu_codes) >= codes.REGION_FREE_MIN_SIGUNGU


def region_rank_expr(sido_code: str | None) -> ColumnElement[int]:
    """시도 전체 > 전국 > 일부 시군구.

    시도 전체 여부는 그 시도 시군구 코드를 SIDO_WIDE_MIN_RATIO 이상 포함하는지로 본다.
    시도의 시군구 수는 단일 시도 정책 중 최대 시군구 수로 구한다.
    """
    if sido_code is None:
        return case((_national(), REGION_NATIONAL), else_=REGION_SIGUNGU)
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
        (_national(), REGION_NATIONAL),
        (in_sido >= func.coalesce(sido_total, 0) * SIDO_WIDE_MIN_RATIO, REGION_SIDO),
        else_=REGION_SIGUNGU,
    )


def _policy_filters(conditions: AppliedConditions) -> list[ColumnElement[bool]]:
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
        filters.append(Policy.sido_codes.contains([conditions.sido_code]))
    else:
        filters.append(_national())
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
    return filters


def policy_query(conditions: AppliedConditions, today: date, limit: int = POLICY_LIMIT) -> Select:
    status = availability_expr(today)
    status_rank = case(
        *((status == s.value, rank) for s, rank in _STATUS_RANK.items()),
        else_=len(_STATUS_RANK),
    )
    region_rank = region_rank_expr(conditions.sido_code)
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
            Policy.apply_url,
            Policy.ref_urls,
            status.label("availability"),
            status_rank.label("status_rank"),
            region_rank.label("region_rank"),
            deadline.label("deadline"),
            func.row_number()
            .over(
                partition_by=(name_key, sigungu_key),
                order_by=(
                    status_rank,
                    region_rank,
                    deadline.asc().nulls_last(),
                    Policy.plcy_no,
                ),
            )
            .label("dup_rank"),
        )
        .where(status != Availability.CLOSED.value, *_policy_filters(conditions))
        .subquery("ranked")
    )
    return (
        select(ranked, func.count().over().label("total"))
        .where(ranked.c.dup_rank == 1)
        .order_by(
            ranked.c.status_rank,
            ranked.c.region_rank,
            ranked.c.deadline.asc().nulls_last(),
            ranked.c.plcy_no,
        )
        .limit(limit)
    )


def saving_query(limit: int = SAVING_LIMIT) -> Select:
    rates = (
        select(
            SavingProductOption.fin_co_no,
            SavingProductOption.fin_prdt_cd,
            func.max(SavingProductOption.max_rate).label("max_rate"),
            func.max(SavingProductOption.base_rate).label("base_rate"),
        )
        .group_by(SavingProductOption.fin_co_no, SavingProductOption.fin_prdt_cd)
        .subquery("rates")
    )
    return (
        select(
            SavingProduct.fin_co_no,
            SavingProduct.fin_prdt_cd,
            SavingProduct.name,
            SavingProduct.company_name,
            SavingProduct.is_youth,
            SavingProduct.join_member,
            SavingProduct.special_condition,
            rates.c.max_rate,
            rates.c.base_rate,
            FinancialCompany.homepage_url,
            func.count().over().label("total"),
        )
        .select_from(SavingProduct)
        .outerjoin(FinancialCompany, FinancialCompany.fin_co_no == SavingProduct.fin_co_no)
        .outerjoin(
            rates,
            and_(
                rates.c.fin_co_no == SavingProduct.fin_co_no,
                rates.c.fin_prdt_cd == SavingProduct.fin_prdt_cd,
            ),
        )
        .order_by(
            SavingProduct.is_youth.desc(),
            rates.c.max_rate.desc().nulls_last(),
            rates.c.base_rate.desc().nulls_last(),
            SavingProduct.fin_co_no,
            SavingProduct.fin_prdt_cd,
        )
        .limit(limit)
    )


def _rate(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


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


def _saving_candidate(row: Mapping[str, Any]) -> SavingCandidate:
    homepage = row["homepage_url"]
    return SavingCandidate(
        fin_co_no=row["fin_co_no"],
        fin_prdt_cd=row["fin_prdt_cd"],
        name=row["name"],
        company_name=row["company_name"],
        is_youth=row["is_youth"],
        max_rate=_rate(row["max_rate"]),
        base_rate=_rate(row["base_rate"]),
        join_member=row["join_member"],
        special_condition=row["special_condition"],
        official_url=homepage,
        url_source=LinkSource.COMPANY_HOMEPAGE if homepage else None,
    )


async def find_candidates(
    conn: AsyncConnection, request: CandidateRequest, today: date
) -> CandidateResponse:
    conditions = resolve_conditions(request)
    policy_rows = (await conn.execute(policy_query(conditions, today))).mappings().all()
    saving_rows = (await conn.execute(saving_query())).mappings().all()
    return CandidateResponse(
        as_of=today,
        conditions=conditions,
        policies=PolicyCandidates(
            total=policy_rows[0]["total"] if policy_rows else 0,
            items=[_policy_candidate(row) for row in policy_rows],
        ),
        savings=SavingCandidates(
            total=saving_rows[0]["total"] if saving_rows else 0,
            items=[_saving_candidate(row) for row in saving_rows],
        ),
    )
