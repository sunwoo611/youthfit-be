"""적재 품질 리포트의 집계·판단.

DB 조회 없이 순수 함수를 검증하고, DB가 있으면 조회 경로도 확인한다.
"""

import json
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import delete, insert, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.core.config import get_settings
from app.ingest import codes
from app.ingest.normalize import normalize_policy, normalize_saving_product
from app.ingest.report import (
    GROUP_ALL,
    GROUP_REGION_FREE,
    GROUP_SEOUL_GYEONGGI,
    HIGH_RATIO,
    REGION_FREE_MIN_SIGUNGU,
    aggregate_policies,
    aggregate_savings,
    is_weak_text,
    judge_crawl,
    load_report,
    policy_groups,
    render_markdown,
)
from app.models import FinancialCompany, Policy, SavingProduct, SavingProductOption

SAMPLES = Path(__file__).resolve().parents[1] / "docs" / "data-samples"

TODAY = date(2026, 10, 26)


def _policy(**overrides: object) -> dict:
    row: dict = {
        "sido_codes": ["11"],
        "sigungu_codes": ["11110"],
        "apply_url": "https://apply.example",
        "ref_urls": ["https://ref.example"],
        "apply_method": "온라인 신청",
        "screening_method": "서류 심사",
        "documents": "신청서",
        "age_limited": True,
        "earn_cond_code": codes.EARN_RANGE,
        "job_codes": ["0013003"],
        "apply_end": date(2026, 12, 31),
        "support_content": "월 20만원을 12개월간 지원한다.",
    }
    row.update(overrides)
    return row


def test_weak_text_is_blank_or_separate_inquiry():
    assert is_weak_text(None)
    assert is_weak_text("  ")
    assert is_weak_text("별도 문의")
    assert is_weak_text("방문 접수. 별도 문의 요망")
    assert not is_weak_text("온라인 신청")
    assert not is_weak_text("문의 게시판")


@pytest.mark.parametrize(
    ("sido", "sigungu_n", "expected"),
    [
        (["11"], 1, {GROUP_ALL, GROUP_SEOUL_GYEONGGI}),
        (["41", "28"], 10, {GROUP_ALL, GROUP_SEOUL_GYEONGGI}),
        (["44"], 5, {GROUP_ALL}),
        (["26"], REGION_FREE_MIN_SIGUNGU, {GROUP_ALL, GROUP_REGION_FREE}),
        (["11"], REGION_FREE_MIN_SIGUNGU, {GROUP_ALL, GROUP_SEOUL_GYEONGGI, GROUP_REGION_FREE}),
        ([], 0, {GROUP_ALL}),
    ],
)
def test_policy_groups(sido: list[str], sigungu_n: int, expected: set[str]):
    sigungu = [f"{i:05d}" for i in range(sigungu_n)]
    assert set(policy_groups(sido, sigungu)) == expected


def test_aggregate_splits_overlapping_groups_and_field_flags():
    national = [f"{i:05d}" for i in range(REGION_FREE_MIN_SIGUNGU)]
    rows = [
        _policy(),
        _policy(
            apply_url=None,
            ref_urls=[],
            apply_method="별도 문의",
            screening_method=None,
            documents="  ",
            age_limited=False,
            earn_cond_code=codes.EARN_ANY,
            job_codes=[codes.JOB_ANY, "0013001"],
            apply_end=date(2026, 10, 25),
            support_content=None,
        ),
        _policy(
            sido_codes=["26"],
            sigungu_codes=national,
            apply_url=None,
            ref_urls=["https://ref.example"],
        ),
        _policy(sido_codes=["44"], sigungu_codes=["44130"], job_codes=[], earn_cond_code=None),
    ]

    groups = aggregate_policies(rows, TODAY)

    assert groups[GROUP_ALL].total == 4
    assert groups[GROUP_SEOUL_GYEONGGI].total == 2
    assert groups[GROUP_REGION_FREE].total == 1

    seoul = groups[GROUP_SEOUL_GYEONGGI]
    assert seoul.no_url == 1
    assert seoul.apply_url_empty == 1
    assert seoul.ref_urls_empty == 1
    assert seoul.apply_method_weak == 1
    assert seoul.screening_method_weak == 1
    assert seoul.documents_weak == 1
    assert seoul.age_unlimited == 1
    assert seoul.earn_unrestricted == 1
    assert seoul.job_unrestricted == 1
    assert seoul.apply_end_present == 2
    assert seoul.expired == 1
    assert seoul.support_empty == 1

    assert groups[GROUP_REGION_FREE].apply_url_empty == 1
    assert groups[GROUP_REGION_FREE].no_url == 0
    assert groups[GROUP_ALL].job_unrestricted == 2
    assert groups[GROUP_ALL].earn_unrestricted == 1


def test_apply_end_on_today_is_not_expired():
    groups = aggregate_policies([_policy(apply_end=TODAY)], TODAY)
    assert groups[GROUP_ALL].apply_end_present == 1
    assert groups[GROUP_ALL].expired == 0


@pytest.mark.parametrize(
    ("text", "bucket"),
    [
        (None, "empty"),
        ("   ", "empty"),
        ("가" * 29, "short"),
        ("가" * 30, "mid"),
        ("가" * 99, "mid"),
        ("가" * 100, "long"),
    ],
)
def test_support_content_length_buckets(text: str | None, bucket: str):
    groups = aggregate_policies([_policy(support_content=text)], TODAY)
    stats = groups[GROUP_ALL]
    assert stats.support_empty == (bucket == "empty")
    assert stats.support_short == (bucket == "short")
    assert stats.support_mid == (bucket == "mid")
    assert stats.support_long == (bucket == "long")


def test_judge_unmeasured_when_seoul_gyeonggi_is_empty():
    groups = aggregate_policies([_policy(sido_codes=["44"], sigungu_codes=["44130"])], TODAY)
    judgment = judge_crawl(groups[GROUP_SEOUL_GYEONGGI])
    assert judgment.code == "unmeasured"


def test_judge_supplement_when_no_url_and_two_core_fields_meet_threshold():
    rows = [_policy(sido_codes=["44"], sigungu_codes=["44130"])]
    rows += [
        _policy(apply_url=None, ref_urls=[], screening_method="별도 문의", documents=None)
        for _ in range(5)
    ]
    rows += [_policy() for _ in range(5)]
    seoul = aggregate_policies(rows, TODAY)[GROUP_SEOUL_GYEONGGI]
    assert seoul.total == 10
    assert seoul.no_url / seoul.total == HIGH_RATIO
    judgment = judge_crawl(seoul)
    assert judgment.code == "supplement"


def test_judge_api_only_when_urls_exist_even_if_core_text_is_weak():
    rows = [
        _policy(screening_method="별도 문의", documents=None, apply_method="별도 문의")
        for _ in range(9)
    ]
    rows.append(_policy(apply_url=None, ref_urls=[]))
    judgment = judge_crawl(aggregate_policies(rows, TODAY)[GROUP_SEOUL_GYEONGGI])
    assert judgment.code == "api_only"


def test_judge_api_only_when_only_one_core_field_is_weak():
    rows = [_policy(apply_url=None, ref_urls=[], documents="별도 문의") for _ in range(6)]
    rows += [_policy() for _ in range(4)]
    judgment = judge_crawl(aggregate_policies(rows, TODAY)[GROUP_SEOUL_GYEONGGI])
    assert judgment.code == "api_only"


def test_render_markdown_includes_rates_criteria_and_decision():
    rows = [
        _policy(apply_url=None, ref_urls=[], screening_method=None, documents="별도 문의")
        for _ in range(5)
    ]
    rows += [_policy() for _ in range(5)]
    policies = aggregate_policies(rows, TODAY)
    savings = aggregate_savings(
        [
            {"is_youth": True, "homepage_url": "https://bank.example"},
            {"is_youth": False, "homepage_url": None},
            {"is_youth": True, "homepage_url": "  "},
        ]
    )
    text = render_markdown(policies, savings, TODAY)

    assert "50.0% (5/10)" in text
    assert "서울·경기" in text
    assert "지역 무관" in text
    assert "50%" in text
    assert "2개 이상" in text
    assert "선별 보완" in text
    assert "66.7% (2/3)" in text
    assert "33.3% (1/3)" in text


def test_aggregate_savings_counts_youth_and_nonblank_homepage():
    stats = aggregate_savings(
        [
            {"is_youth": True, "homepage_url": "https://bank.example"},
            {"is_youth": False, "homepage_url": None},
            {"is_youth": True, "homepage_url": "  "},
        ]
    )
    assert stats.products == 3
    assert stats.youth == 2
    assert stats.homepage_mapped == 1


@pytest.fixture
async def conn() -> AsyncIterator[AsyncConnection]:
    url = get_settings().database_url
    if not url:
        pytest.skip("DATABASE_URL 미설정")
    engine = create_async_engine(url, connect_args={"timeout": 2})
    try:
        connection = await engine.connect()
    except (OSError, TimeoutError) as exc:
        await engine.dispose()
        pytest.skip(f"DB 연결 불가: {type(exc).__name__}")
    trans = await connection.begin()
    try:
        if await connection.scalar(text("SELECT to_regclass('public.policies')")) is None:
            pytest.skip("스키마 미적용 (uv run alembic upgrade head)")
        for table in (
            SavingProductOption.__table__,
            SavingProduct.__table__,
            FinancialCompany.__table__,
            Policy.__table__,
        ):
            await connection.execute(delete(table))
        yield connection
    finally:
        await trans.rollback()
        await connection.close()
        await engine.dispose()


def _sample_policy(plcy_no: str) -> dict:
    body = json.loads((SAMPLES / "ontong-getPlcy-sample.json").read_text(encoding="utf-8"))
    raw = {**body["result"]["youthPolicyList"][0], "plcyNo": plcy_no}
    return normalize_policy(raw)


async def test_load_report_reads_policy_and_saving_rows(conn: AsyncConnection):
    policy = _sample_policy("QR-1")
    policy["sido_codes"] = ["11"]
    policy["sigungu_codes"] = ["11110"]
    policy["apply_url"] = None
    policy["ref_urls"] = []
    policy["apply_method"] = "별도 문의"
    policy["screening_method"] = "별도 문의"
    policy["documents"] = None
    await conn.execute(insert(Policy.__table__), policy)
    await conn.execute(
        insert(FinancialCompany.__table__),
        {
            "fin_co_no": "QRCO",
            "fin_grp_code": "020000",
            "name": "테스트은행",
            "homepage_url": "https://bank.example",
            "call_center": None,
            "raw": {"fin_co_no": "QRCO"},
        },
    )
    youth = normalize_saving_product(
        {
            "fin_co_no": "QRCO",
            "fin_prdt_cd": "QR-Y",
            "fin_prdt_nm": "청년 적금",
            "join_member": "청년",
        },
        "020000",
    )
    orphan = normalize_saving_product(
        {"fin_co_no": "MISSING", "fin_prdt_cd": "QR-N", "fin_prdt_nm": "일반 적금"},
        "020000",
    )
    await conn.execute(insert(SavingProduct.__table__), [youth, orphan])

    policies, savings, _today = await load_report(conn, TODAY)

    seoul = policies[GROUP_SEOUL_GYEONGGI]
    assert seoul.total == 1
    assert seoul.no_url == 1
    assert seoul.apply_method_weak == 1
    assert seoul.screening_method_weak == 1
    assert seoul.documents_weak == 1
    assert policies[GROUP_ALL].total == 1
    assert savings.products == 2
    assert savings.youth == 1
    assert savings.homepage_mapped == 1
