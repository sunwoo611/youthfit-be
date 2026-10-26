"""1차 룰 필터. 순수 함수는 DB 없이, 필터·정렬·중복 제거는 롤백 트랜잭션에서 검증한다."""

import itertools
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from app.api.deps import get_db_connection, get_today
from app.ingest import codes
from app.main import app
from app.models import FinancialCompany, Policy, SavingProduct, SavingProductOption
from app.schemas.candidates import (
    AppliedConditions,
    CandidateRequest,
    EmploymentStatus,
    Interest,
    LinkSource,
)
from app.services.candidate_service import (
    ONTONG_DETAIL_URL,
    POLICY_LIMIT,
    SAVING_LIMIT,
    availability_expr,
    find_candidates,
    policy_official_link,
    resolve_conditions,
)
from app.services.policy_status import Availability, policy_availability

TODAY = date(2026, 10, 26)
PAST = date(2026, 10, 25)
FUTURE = date(2026, 10, 27)
NATIONAL = [f"{i:05d}" for i in range(codes.REGION_FREE_MIN_SIGUNGU)]


# --- 순수 함수 ---


def test_official_link_prefers_apply_then_reference_then_ontong_detail():
    assert policy_official_link("P1", "https://apply.example", ["https://ref.example"]) == (
        "https://apply.example",
        LinkSource.APPLY,
    )
    assert policy_official_link("P1", None, ["", "https://ref.example"]) == (
        "https://ref.example",
        LinkSource.REFERENCE,
    )
    assert policy_official_link("P1", None, []) == (
        ONTONG_DETAIL_URL.format("P1"),
        LinkSource.ONTONG_DETAIL,
    )


def test_explicit_fields_override_query_hints():
    conditions = resolve_conditions(
        CandidateRequest(
            age=30,
            employment_status=EmploymentStatus.EMPLOYED,
            query="경기 사는 25살 백수 월세",
        )
    )
    assert conditions.age == 30
    assert conditions.employment_status is EmploymentStatus.EMPLOYED
    assert conditions.sido_code == "41"
    assert conditions.interests == [Interest.HOUSING]
    assert conditions.from_query == ["sido_code", "interests"]


def test_explicit_interests_are_not_extended_by_query():
    conditions = resolve_conditions(
        CandidateRequest(interests=[Interest.JOBS, Interest.JOBS], query="월세")
    )
    assert conditions.interests == [Interest.JOBS]
    assert conditions.from_query == []


def test_unknown_sido_is_rejected():
    with pytest.raises(ValidationError):
        CandidateRequest(sido_code="29")


# --- DB 통합 ---


def _policy(plcy_no: str, **overrides: object) -> dict:
    row: dict = {
        "plcy_no": plcy_no,
        "name": f"정책 {plcy_no}",
        "categories": ["일자리"],
        "subcategories": ["취업"],
        "age_limited": True,
        "min_age": 19,
        "max_age": 34,
        "job_codes": [],
        "sido_codes": ["11"],
        "sigungu_codes": ["11110"],
        "apply_period_code": codes.APPLY_PERIOD_FIXED,
        "apply_start": date(2026, 10, 1),
        "apply_end": date(2026, 12, 31),
        "biz_start": None,
        "biz_end": None,
        "apply_url": None,
        "ref_urls": [],
        "org_name": "테스트기관",
        "raw": {"plcyNo": plcy_no},
    }
    row.update(overrides)
    return row


async def _insert_policies(conn: AsyncConnection, *rows: dict) -> None:
    await conn.execute(insert(Policy.__table__), list(rows))


async def _policy_nos(conn: AsyncConnection, request: CandidateRequest) -> list[str]:
    result = await find_candidates(conn, request, TODAY)
    return [item.plcy_no for item in result.policies.items]


async def test_sql_availability_matches_python(db_conn: AsyncConnection):
    period_codes = [
        codes.APPLY_PERIOD_FIXED,
        codes.APPLY_PERIOD_ALWAYS,
        codes.APPLY_PERIOD_UNSPECIFIED,
        None,
    ]
    dates = [None, PAST, TODAY, FUTURE]
    rows = []
    expected = {}
    for i, (code, a_start, a_end, b_start, b_end) in enumerate(
        itertools.product(period_codes, dates, dates, dates, dates)
    ):
        plcy_no = f"AV-{i:04d}"
        rows.append(
            _policy(
                plcy_no,
                apply_period_code=code,
                apply_start=a_start,
                apply_end=a_end,
                biz_start=b_start,
                biz_end=b_end,
            )
        )
        expected[plcy_no] = policy_availability(
            code,
            apply_start=a_start,
            apply_end=a_end,
            biz_start=b_start,
            biz_end=b_end,
            today=TODAY,
        )
    await _insert_policies(db_conn, *rows)

    result = await db_conn.execute(select(Policy.plcy_no, availability_expr(TODAY)))
    actual = {plcy_no: Availability(status) for plcy_no, status in result.all()}

    assert len(actual) == len(period_codes) * len(dates) ** 4
    assert actual == expected


async def test_filters_age_region_employment_and_interest(db_conn: AsyncConnection):
    await _insert_policies(
        db_conn,
        _policy("OK-BASE"),
        _policy("OK-NO-AGE-LIMIT", age_limited=False, min_age=None, max_age=None),
        _policy("OK-JOB-ANY", job_codes=[codes.JOB_ANY]),
        _policy("OK-JOB-MATCH", job_codes=["0013003", "0013001"]),
        _policy("OK-NATIONAL", sido_codes=["11", "41"], sigungu_codes=NATIONAL),
        _policy("OK-EDU-2025", categories=["교육"], subcategories=["미래역량강화"]),
        _policy("NG-TOO-OLD", min_age=19, max_age=24),
        _policy("NG-TOO-YOUNG", min_age=30, max_age=39),
        _policy("NG-GYEONGGI", sido_codes=["41"], sigungu_codes=["41110"]),
        _policy("NG-EMPLOYED-ONLY", job_codes=["0013001"]),
        _policy("NG-HOUSING", categories=["주거"], subcategories=["주택 및 거주지"]),
        _policy("NG-CLOSED", apply_end=PAST),
        _policy("NG-BIZ-ENDED", apply_period_code=codes.APPLY_PERIOD_ALWAYS, biz_end=PAST),
    )
    request = CandidateRequest(
        age=25,
        sido_code="11",
        employment_status=EmploymentStatus.UNEMPLOYED,
        interests=[Interest.JOBS, Interest.EDUCATION],
    )
    assert set(await _policy_nos(db_conn, request)) == {
        "OK-BASE",
        "OK-NO-AGE-LIMIT",
        "OK-JOB-ANY",
        "OK-JOB-MATCH",
        "OK-NATIONAL",
        "OK-EDU-2025",
    }


async def test_without_region_only_national_policies(db_conn: AsyncConnection):
    await _insert_policies(
        db_conn,
        _policy("LOCAL"),
        _policy("NATIONAL", sido_codes=["11", "41"], sigungu_codes=NATIONAL),
    )
    assert await _policy_nos(db_conn, CandidateRequest()) == ["NATIONAL"]


async def test_finance_interest_requires_finance_subcategory(db_conn: AsyncConnection):
    await _insert_policies(
        db_conn,
        _policy("FIN", categories=["금융･복지･문화"], subcategories=["취약계층 및 금융지원"]),
        _policy("FIN-2025", categories=["복지문화"], subcategories=["취약계층 및 금융지원"]),
        _policy("CULTURE", categories=["금융･복지･문화"], subcategories=["문화활동 및 생활지원"]),
    )
    finance = CandidateRequest(sido_code="11", interests=[Interest.FINANCE])
    welfare = CandidateRequest(sido_code="11", interests=[Interest.WELFARE])
    assert set(await _policy_nos(db_conn, finance)) == {"FIN", "FIN-2025"}
    assert set(await _policy_nos(db_conn, welfare)) == {"FIN", "FIN-2025", "CULTURE"}


async def test_orders_by_status_then_region_then_deadline(db_conn: AsyncConnection):
    unspecified = codes.APPLY_PERIOD_UNSPECIFIED
    await _insert_policies(
        db_conn,
        _policy("UNVERIFIED", apply_period_code=unspecified, apply_start=None, apply_end=None),
        _policy("UPCOMING", apply_start=FUTURE),
        _policy(
            "OPEN-NATIONAL-SOON",
            sido_codes=["11", "41"],
            sigungu_codes=NATIONAL,
            apply_end=date(2026, 10, 28),
        ),
        _policy("OPEN-LOCAL-LATE", apply_end=date(2026, 12, 31)),
        _policy("OPEN-LOCAL-SOON", apply_end=date(2026, 11, 1)),
        _policy(
            "OPEN-LOCAL-ALWAYS",
            apply_period_code=codes.APPLY_PERIOD_ALWAYS,
            apply_start=None,
            apply_end=None,
        ),
    )
    result = await find_candidates(db_conn, CandidateRequest(sido_code="11"), TODAY)
    assert [item.plcy_no for item in result.policies.items] == [
        "OPEN-LOCAL-SOON",
        "OPEN-LOCAL-LATE",
        "OPEN-LOCAL-ALWAYS",
        "OPEN-NATIONAL-SOON",
        "UPCOMING",
        "UNVERIFIED",
    ]
    statuses = [item.availability for item in result.policies.items]
    assert statuses[-2:] == [Availability.UPCOMING, Availability.UNVERIFIED]
    assert result.policies.items[3].eligibility.region_scope == "national"


async def test_partial_sigungu_policies_rank_after_national(db_conn: AsyncConnection):
    seoul_all = [f"11{i:03d}" for i in range(25)]
    await _insert_policies(
        db_conn,
        _policy("SIGUNGU-SOON", sigungu_codes=["11110"], apply_end=date(2026, 10, 27)),
        _policy("SIGUNGU-12", sigungu_codes=seoul_all[:12]),
        _policy("NATIONAL", sido_codes=["11", "41"], sigungu_codes=NATIONAL),
        _policy("SIDO-23", sigungu_codes=seoul_all[:23]),
        _policy("SIDO-ALL", sigungu_codes=seoul_all),
        _policy("OTHER-SIDO-ALL", sido_codes=["41"], sigungu_codes=["41110"] * 40),
    )
    result = await find_candidates(db_conn, CandidateRequest(sido_code="11"), TODAY)
    items = result.policies.items
    assert [(item.plcy_no, item.eligibility.region_scope) for item in items] == [
        ("SIDO-23", "sido"),
        ("SIDO-ALL", "sido"),
        ("NATIONAL", "national"),
        ("SIGUNGU-SOON", "sigungu"),
        ("SIGUNGU-12", "sigungu"),
    ]
    assert items[3].eligibility.sigungu_codes == ["11110"]
    assert items[0].eligibility.sigungu_codes == []
    assert items[2].eligibility.sigungu_codes == []


async def test_dedup_keeps_best_ranked_record_per_normalized_name(db_conn: AsyncConnection):
    await _insert_policies(
        db_conn,
        _policy(
            "DUP-NATIONAL",
            name="국민취업지원제도",
            sido_codes=["11", "41"],
            sigungu_codes=NATIONAL,
            apply_period_code=codes.APPLY_PERIOD_ALWAYS,
            apply_start=None,
            apply_end=None,
        ),
        _policy(
            "DUP-UNVERIFIED",
            name="국민취업지원제도",
            apply_period_code=codes.APPLY_PERIOD_UNSPECIFIED,
            apply_start=None,
            apply_end=None,
        ),
        _policy("SPACE-A", name="청년 한부모 자립 지원(추가 아동 양육비 지원)"),
        _policy("SPACE-B", name="청년한부모 자립 지원(추가아동양육비 지원)", apply_start=FUTURE),
        _policy("OTHER", name="청년 월세 지원"),
    )
    result = await find_candidates(db_conn, CandidateRequest(sido_code="11"), TODAY)
    assert {item.plcy_no for item in result.policies.items} == {
        "DUP-NATIONAL",
        "SPACE-A",
        "OTHER",
    }
    assert result.policies.total == 3


async def test_policy_limit_and_total(db_conn: AsyncConnection):
    await _insert_policies(db_conn, *(_policy(f"LIM-{i:03d}") for i in range(POLICY_LIMIT + 5)))
    result = await find_candidates(db_conn, CandidateRequest(sido_code="11"), TODAY)
    assert len(result.policies.items) == POLICY_LIMIT
    assert result.policies.total == POLICY_LIMIT + 5


async def test_policy_card_uses_db_links_and_eligibility(db_conn: AsyncConnection):
    await _insert_policies(
        db_conn,
        _policy("APPLY", apply_url="https://apply.example", ref_urls=["https://ref.example"]),
        _policy("REF", ref_urls=["https://ref.example"], job_codes=["0013003"]),
        _policy("NONE", job_codes=[codes.JOB_ANY]),
    )
    result = await find_candidates(db_conn, CandidateRequest(sido_code="11"), TODAY)
    cards = {item.plcy_no: item for item in result.policies.items}
    assert (cards["APPLY"].official_url, cards["APPLY"].url_source) == (
        "https://apply.example",
        LinkSource.APPLY,
    )
    assert cards["REF"].url_source is LinkSource.REFERENCE
    assert cards["REF"].eligibility.employment == ["미취업자"]
    assert cards["NONE"].official_url == ONTONG_DETAIL_URL.format("NONE")
    assert cards["NONE"].eligibility.employment == []
    assert cards["NONE"].eligibility.min_age == 19


async def _insert_saving(
    conn: AsyncConnection, code: str, *, youth: bool, max_rate: str | None, fin_co_no: str = "CO"
) -> None:
    await conn.execute(
        insert(SavingProduct.__table__),
        {
            "fin_co_no": fin_co_no,
            "fin_prdt_cd": code,
            "fin_grp_code": "020000",
            "company_name": "테스트은행",
            "name": f"적금 {code}",
            "is_youth": youth,
            "raw": {},
        },
    )
    if max_rate is None:
        return
    await conn.execute(
        insert(SavingProductOption.__table__),
        [
            {
                "fin_co_no": fin_co_no,
                "fin_prdt_cd": code,
                "rate_type": "S",
                "reserve_type": "S",
                "term_months": term,
                "base_rate": Decimal("2.00"),
                "max_rate": Decimal(max_rate) - Decimal(offset),
            }
            for term, offset in ((12, "0"), (24, "0.5"))
        ],
    )


async def test_savings_youth_first_then_max_rate(db_conn: AsyncConnection):
    await db_conn.execute(
        insert(FinancialCompany.__table__),
        {
            "fin_co_no": "CO",
            "fin_grp_code": "020000",
            "name": "테스트은행",
            "homepage_url": "https://bank.example",
            "raw": {},
        },
    )
    await _insert_saving(db_conn, "GENERAL-HIGH", youth=False, max_rate="9.00")
    await _insert_saving(db_conn, "YOUTH-LOW", youth=True, max_rate="4.00")
    await _insert_saving(db_conn, "YOUTH-HIGH", youth=True, max_rate="6.00")
    await _insert_saving(db_conn, "NO-RATE", youth=False, max_rate=None)
    await _insert_saving(db_conn, "ORPHAN", youth=False, max_rate="3.00", fin_co_no="NOCO")
    for i in range(SAVING_LIMIT):
        await _insert_saving(db_conn, f"FILL-{i:02d}", youth=False, max_rate="1.00")

    result = await find_candidates(db_conn, CandidateRequest(), TODAY)
    items = result.savings.items
    assert result.savings.total == SAVING_LIMIT + 5
    assert len(items) == SAVING_LIMIT
    assert [item.fin_prdt_cd for item in items[:4]] == [
        "YOUTH-HIGH",
        "YOUTH-LOW",
        "GENERAL-HIGH",
        "ORPHAN",
    ]
    assert items[0].max_rate == 6.0
    assert items[0].base_rate == 2.0
    assert (items[0].official_url, items[0].url_source) == (
        "https://bank.example",
        LinkSource.COMPANY_HOMEPAGE,
    )
    assert (items[3].official_url, items[3].url_source) == (None, None)
    assert "NO-RATE" not in {item.fin_prdt_cd for item in items}


@pytest.fixture
async def client(db_conn: AsyncConnection) -> AsyncIterator[AsyncClient]:
    async def _conn() -> AsyncIterator[AsyncConnection]:
        yield db_conn

    app.dependency_overrides[get_db_connection] = _conn
    app.dependency_overrides[get_today] = lambda: TODAY
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.clear()


async def test_candidates_endpoint(client: AsyncClient, db_conn: AsyncConnection):
    await _insert_policies(db_conn, _policy("API-1", apply_url="https://apply.example"))

    res = await client.post("/v1/candidates", json={"query": "서울 사는 25살 취준생"})

    assert res.status_code == 200
    body = res.json()
    assert body["as_of"] == TODAY.isoformat()
    assert body["conditions"] == AppliedConditions(
        age=25,
        sido_code="11",
        employment_status=EmploymentStatus.UNEMPLOYED,
        interests=[Interest.JOBS],
        from_query=["age", "sido_code", "employment_status", "interests"],
    ).model_dump(mode="json")
    assert body["policies"]["total"] == 1
    card = body["policies"]["items"][0]
    assert card["official_url"] == "https://apply.example"
    assert card["availability"] == "open"
    assert body["savings"] == {"total": 0, "items": []}


async def test_candidates_endpoint_rejects_unknown_sido(client: AsyncClient):
    res = await client.post("/v1/candidates", json={"sido_code": "99"})
    assert res.status_code == 422
