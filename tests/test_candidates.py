"""1차 룰 필터. 순수 함수는 DB 없이, 필터·정렬·중복 제거는 롤백 트랜잭션에서 검증한다."""

import itertools
from collections.abc import AsyncIterator
from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from app.api.deps import get_db_connection, get_today
from app.ingest import codes, sigungu
from app.main import app
from app.models import Policy
from app.schemas.candidates import (
    AppliedConditions,
    CandidateRequest,
    EmploymentStatus,
    IncomeBucket,
    Interest,
    LinkSource,
    Marriage,
    School,
)
from app.services.candidate_service import (
    ONTONG_DETAIL_URL,
    POLICY_LIMIT,
    availability_expr,
    find_candidates,
    policy_official_link,
    resolve_conditions,
    search_candidates,
)
from app.services.policy_status import Availability, policy_availability
from app.services.region_scope import (
    is_basic_local_org,
    is_effective_national,
    is_fake_national,
    is_national_zip,
)

TODAY = date(2026, 10, 26)
PAST = date(2026, 10, 25)
FUTURE = date(2026, 10, 27)
NATIONAL = [f"{i:05d}" for i in range(codes.REGION_FREE_MIN_SIGUNGU)]


def _mixed_national_zip(*, focus_sido: str = "47") -> list[str]:
    """법정 앞2자리가 섞인 전국 zip. focus 시도 시군구를 전부 넣어 sido 오분류를 유도한다."""
    codes_list = list(sigungu.SIGUNGU[focus_sido])
    for sido, names in sigungu.SIGUNGU.items():
        if sido == focus_sido:
            continue
        codes_list.extend(names)
        if len(codes_list) >= codes.REGION_FREE_MIN_SIGUNGU:
            break
    pad = codes.REGION_FREE_MIN_SIGUNGU - len(codes_list)
    if pad > 0:
        codes_list.extend(f"99{i:03d}" for i in range(pad))
    return codes_list[: max(len(codes_list), codes.REGION_FREE_MIN_SIGUNGU)]


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


def _conditions(sido_code: str | None = "11", **overrides: object) -> AppliedConditions:
    return AppliedConditions(
        age=None,
        sido_code=sido_code,
        employment_status=None,
        interests=[],
        from_query=[],
        **overrides,
    )


def test_question_conditions_default_to_unknown():
    conditions = resolve_conditions(CandidateRequest(query="서울 사는 25살 취준생"))
    assert conditions.sigungu_codes == []
    assert (conditions.marriage, conditions.school, conditions.income_bucket) == (None,) * 3


@pytest.mark.parametrize(
    ("org_name", "expected"),
    [
        ("경상북도 의성군 관광복지과", True),
        ("의성군청", True),
        ("서울특별시 강남구청", True),
        ("수원시청 청년정책과", True),
        ("경기도청", False),
        ("고용노동부", False),
        ("테스트기관", False),
        (None, False),
        ("", False),
    ],
)
def test_is_basic_local_org(org_name: str | None, expected: bool):
    assert is_basic_local_org(org_name) is expected


def test_national_zip_and_effective_national_helpers():
    assert is_national_zip(NATIONAL)
    assert not is_national_zip(NATIONAL[:-1])
    assert is_effective_national(NATIONAL, "고용노동부")
    assert not is_effective_national(NATIONAL, "경상북도 의성군 관광복지과")
    assert is_fake_national(NATIONAL, "경상북도 의성군 관광복지과")
    assert not is_fake_national(NATIONAL, "경기도청")


@pytest.mark.parametrize(
    ("sido_code", "sigungu_codes"),
    [("11", ["41110"]), (None, ["11110"]), ("11", ["1111"]), ("11", ["11a10"])],
)
def test_sigungu_outside_sido_is_rejected(sido_code: str | None, sigungu_codes: list[str]):
    with pytest.raises(ValidationError):
        _conditions(sido_code, sigungu_codes=sigungu_codes)


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


async def _searched_nos(conn: AsyncConnection, conditions: AppliedConditions) -> list[str]:
    result = await search_candidates(conn, conditions, TODAY)
    return [item.plcy_no for item in result.policies.items]


async def test_marriage_filter_keeps_unrestricted(db_conn: AsyncConnection):
    await _insert_policies(
        db_conn,
        _policy("M-NULL", marriage_code=None),
        _policy("M-ANY", marriage_code=codes.MARRIAGE_ANY),
        _policy("M-SINGLE", marriage_code="0055002"),
        _policy("M-MARRIED", marriage_code="0055001"),
    )
    single = _conditions(marriage=Marriage.SINGLE)
    married = _conditions(marriage=Marriage.MARRIED)
    assert set(await _searched_nos(db_conn, single)) == {"M-NULL", "M-ANY", "M-SINGLE"}
    assert set(await _searched_nos(db_conn, married)) == {"M-NULL", "M-ANY", "M-MARRIED"}


async def test_school_filter_treats_etc_as_unrestricted(db_conn: AsyncConnection):
    await _insert_policies(
        db_conn,
        _policy("S-EMPTY", school_codes=[]),
        _policy("S-ANY", school_codes=[codes.SCHOOL_ANY]),
        _policy("S-ETC", school_codes=[codes.SCHOOL_ETC]),
        _policy("S-MATCH", school_codes=["0049005", "0049007"]),
        _policy("S-HIGH-ONLY", school_codes=["0049004"]),
    )
    conditions = _conditions(school=School.COLLEGE_ENROLLED)
    assert set(await _searched_nos(db_conn, conditions)) == {
        "S-EMPTY",
        "S-ANY",
        "S-ETC",
        "S-MATCH",
    }


@pytest.mark.parametrize(
    ("school", "expected"),
    [
        # 졸업 예정자는 재학 중이라 재학 대상 정책도 받는다.
        (School.HIGH_SCHOOL_EXPECTED, {"S-HS-ENROLLED", "S-GRADUATES"}),
        (School.COLLEGE_EXPECTED, {"S-COLLEGE-ENROLLED", "S-GRADUATES"}),
        # 석·박사는 학사를 가지므로 대졸 대상(특성화대학원 등)을 받는다.
        (School.GRADUATE_SCHOOL, {"S-COLLEGE-GRADUATED", "S-GRADUATES"}),
        # 재학생은 고졸로 넓히지 않는다(재학생 제외 정책이 대학 재학만 빼고 나열).
        (School.COLLEGE_ENROLLED, {"S-COLLEGE-ENROLLED"}),
        # 대졸은 고졸로 넓히지 않는다(고졸 전용 정책).
        (School.COLLEGE_GRADUATED, {"S-COLLEGE-GRADUATED", "S-GRADUATES"}),
    ],
)
async def test_school_answer_covers_contained_statuses_only(
    db_conn: AsyncConnection, school: School, expected: set[str]
):
    await _insert_policies(
        db_conn,
        _policy("S-HS-ENROLLED", school_codes=["0049002"]),
        _policy("S-HS-ONLY", school_codes=["0049004"]),
        _policy("S-COLLEGE-ENROLLED", school_codes=["0049005"]),
        _policy("S-COLLEGE-GRADUATED", school_codes=["0049007"]),
        _policy("S-GRADUATES", school_codes=["0049003", "0049004", "0049006", "0049007"]),
    )
    assert set(await _searched_nos(db_conn, _conditions(school=school))) == expected


async def test_income_filter_uses_only_trusted_range_max(db_conn: AsyncConnection):
    def earn(code: str | None, earn_max: int | None) -> dict:
        return {"earn_cond_code": code, "earn_max": earn_max}

    await _insert_policies(
        db_conn,
        _policy("I-NULL", **earn(None, None)),
        _policy("I-ANY", **earn(codes.EARN_ANY, None)),
        _policy("I-ETC", **earn("0043003", None)),
        _policy("I-RANGE-NULL", **earn(codes.EARN_RANGE, None)),
        _policy("I-ABOVE", **earn(codes.EARN_RANGE, 3500)),
        _policy("I-EQUAL", **earn(codes.EARN_RANGE, 3000)),
        _policy("I-BELOW", **earn(codes.EARN_RANGE, 2000)),
        _policy("I-WON", **earn(codes.EARN_RANGE, 43_056_240)),
        _policy("I-MONTHLY", **earn(codes.EARN_RANGE, 534)),
    )
    everyone = {
        "I-NULL",
        "I-ANY",
        "I-ETC",
        "I-RANGE-NULL",
        "I-ABOVE",
        "I-EQUAL",
        "I-BELOW",
        "I-WON",
        "I-MONTHLY",
    }
    low = _conditions(income_bucket=IncomeBucket.UP_TO_3000)
    middle = _conditions(income_bucket=IncomeBucket.UP_TO_5000)
    assert set(await _searched_nos(db_conn, low)) == everyone
    assert set(await _searched_nos(db_conn, middle)) == everyone - {"I-EQUAL", "I-BELOW"}


async def test_sigungu_filters_partial_policies_and_ranks_mine_with_sido(
    db_conn: AsyncConnection,
):
    seoul_all = ["11110", *(f"11{i:03d}" for i in range(200, 224))]
    await _insert_policies(
        db_conn,
        _policy("SIDO-ALL", sigungu_codes=seoul_all),
        _policy("SIDO-NOT-MINE", sigungu_codes=seoul_all[1:]),
        _policy("MINE", sigungu_codes=["11110", "11200"], apply_end=date(2026, 11, 1)),
        _policy("OTHER", sigungu_codes=["11200"], apply_end=date(2026, 11, 1)),
        _policy("NATIONAL", sido_codes=["11", "41"], sigungu_codes=NATIONAL),
    )
    result = await search_candidates(db_conn, _conditions(sigungu_codes=["11110"]), TODAY)
    # 시도 전체 정책도 내 시군구가 빠져 있으면 지운다. 전국 정책은 남긴다.
    assert [(item.plcy_no, item.eligibility.region_scope) for item in result.policies.items] == [
        ("MINE", "sigungu"),
        ("SIDO-ALL", "sido"),
        ("NATIONAL", "national"),
    ]

    unknown = await _searched_nos(db_conn, _conditions())
    assert unknown == ["SIDO-ALL", "SIDO-NOT-MINE", "NATIONAL", "MINE", "OTHER"]


async def test_without_region_only_national_policies(db_conn: AsyncConnection):
    await _insert_policies(
        db_conn,
        _policy("LOCAL"),
        _policy("NATIONAL", sido_codes=["11", "41"], sigungu_codes=NATIONAL),
    )
    assert await _policy_nos(db_conn, CandidateRequest()) == ["NATIONAL"]


def _fake_national_uiseong(**overrides: object) -> dict:
    zip_codes = overrides.pop("sigungu_codes", _mixed_national_zip())
    return _policy(
        "FAKE-UISEONG",
        name="의성군 청년 주거 지원",
        org_name="경상북도 의성군 관광복지과",
        sido_codes=["11", "41", "47"],
        sigungu_codes=zip_codes,
        categories=["주거"],
        subcategories=["주택 및 거주지"],
        **overrides,
    )


async def test_fake_national_excluded_without_sido_and_other_sido(
    db_conn: AsyncConnection,
):
    await _insert_policies(
        db_conn,
        _fake_national_uiseong(),
        _policy(
            "TRUE-NATIONAL",
            sido_codes=["11", "41", "47"],
            sigungu_codes=NATIONAL,
            org_name="고용노동부",
        ),
        _policy(
            "METRO-ZIP",
            sido_codes=["11", "41", "47"],
            sigungu_codes=NATIONAL,
            org_name="경기도청",
        ),
    )
    assert set(await _policy_nos(db_conn, CandidateRequest())) == {
        "TRUE-NATIONAL",
        "METRO-ZIP",
    }
    assert "FAKE-UISEONG" not in await _policy_nos(
        db_conn, CandidateRequest(sido_code="11")
    )


async def test_fake_national_kept_for_matching_sido_as_sigungu(
    db_conn: AsyncConnection,
):
    # 경북 단일 시도 정책이 있어야 sido_total이 잡히고, 가짜 전국 zip이
    # 경북 시군구를 대부분 포함해도 region_scope 는 sigungu 로 남아야 한다.
    gyeongbuk = list(sigungu.SIGUNGU["47"])
    mixed = _mixed_national_zip(focus_sido="47")
    assert len(mixed) >= codes.REGION_FREE_MIN_SIGUNGU
    await _insert_policies(
        db_conn,
        _policy("SIDO-GYEONGBUK", sido_codes=["47"], sigungu_codes=gyeongbuk),
        _fake_national_uiseong(sigungu_codes=mixed),
    )
    result = await find_candidates(db_conn, CandidateRequest(sido_code="47"), TODAY)
    fake = next(item for item in result.policies.items if item.plcy_no == "FAKE-UISEONG")
    assert fake.eligibility.region_scope == "sigungu"
    assert fake.eligibility.sigungu_codes == mixed


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


async def test_dedup_keeps_same_name_programs_of_different_sigungu(db_conn: AsyncConnection):
    gyeongbuk_all = [f"47{i:03d}" for i in range(23)]
    await _insert_policies(
        db_conn,
        _policy("SIDO-WIDE", sido_codes=["47"], sigungu_codes=gyeongbuk_all),
        _policy("CHILGOK", name="청년 이사비 지원사업", sido_codes=["47"], sigungu_codes=["47850"]),
        _policy("ULJIN", name="청년 이사비 지원사업", sido_codes=["47"], sigungu_codes=["47930"]),
        _policy(
            "ULJIN-DUP",
            name="청년이사비 지원사업",
            sido_codes=["47"],
            sigungu_codes=["47930"],
            apply_start=FUTURE,
        ),
    )
    result = await find_candidates(db_conn, CandidateRequest(sido_code="47"), TODAY)
    assert {item.plcy_no for item in result.policies.items} == {"SIDO-WIDE", "CHILGOK", "ULJIN"}
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
    assert "savings" not in body


async def test_candidates_endpoint_rejects_unknown_sido(client: AsyncClient):
    res = await client.post("/v1/candidates", json={"sido_code": "99"})
    assert res.status_code == 422
