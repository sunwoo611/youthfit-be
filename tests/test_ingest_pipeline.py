import asyncio
import json
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from app.ingest import loader, pipeline
from app.ingest.fss import FssListing
from app.ingest.http import IngestAPIError
from app.ingest.loader import PruneResult, prune_allowed
from app.ingest.ontong import OntongFetchResult
from app.ingest.pipeline import (
    SourceReport,
    format_report,
    is_complete_fetch,
    prepare_companies,
    prepare_policies,
    prepare_saving,
    run_fss,
    run_ontong,
)

SAMPLES = Path(__file__).resolve().parents[1] / "docs" / "data-samples"


def _ontong() -> list[dict]:
    body = json.loads((SAMPLES / "ontong-getPlcy-sample.json").read_text(encoding="utf-8"))
    return body["result"]["youthPolicyList"]


def _fss() -> dict:
    body = json.loads(
        (SAMPLES / "fss-savingProductsSearch-sample.json").read_text(encoding="utf-8")
    )
    return body["result"]


def test_prepare_policies_skips_invalid_dedupes_and_counts_unknown_codes():
    a, b, c = _ontong()
    raws = [
        a,
        b,
        {**b, "plcyNm": "갱신된 이름"},
        {**c, "jobCd": "0013099"},
        {"plcyNo": "", "plcyNm": "번호 없음"},
    ]

    prepared, unknown = prepare_policies(raws)

    assert [r["plcy_no"] for r in prepared.rows] == [a["plcyNo"], b["plcyNo"], c["plcyNo"]]
    assert prepared.rows[1]["name"] == "갱신된 이름"
    assert prepared.duplicates == 1
    assert prepared.skipped == 1
    assert len(prepared.error_samples) == 1
    assert unknown == Counter({("job_codes", "0013099"): 1})


def test_prepare_saving_drops_orphan_options_and_duplicates():
    result = _fss()
    options = result["optionList"]
    listing = FssListing(
        fin_grp_code="020000",
        base_list=result["baseList"][:1],
        option_list=[*options, options[0]],
        total_count=1,
    )

    prepared = prepare_saving(listing)

    assert [p["fin_prdt_cd"] for p in prepared.products.rows] == ["WR0001F"]
    assert all(o["fin_prdt_cd"] == "WR0001F" for o in prepared.options.rows)
    assert len(prepared.options.rows) == 6
    assert prepared.options.duplicates == 1
    assert prepared.orphan_options == 2
    assert all(p["fin_grp_code"] == "020000" for p in prepared.products.rows)


def test_prepare_companies_sets_group_code():
    listing = FssListing(
        fin_grp_code="030300",
        base_list=[
            {"fin_co_no": "0010300", "kor_co_nm": "테스트저축은행", "homp_url": "www.test.co.kr"},
            {"fin_co_no": "", "kor_co_nm": "번호 없음"},
        ],
    )

    prepared = prepare_companies(listing)

    assert prepared.rows == [
        {
            "fin_co_no": "0010300",
            "fin_grp_code": "030300",
            "name": "테스트저축은행",
            "homepage_url": "http://www.test.co.kr",
            "call_center": None,
            "raw": listing.base_list[0],
        }
    ]
    assert prepared.skipped == 1


def test_format_report_includes_counts_unknown_codes_and_notes():
    report = SourceReport(
        source="ontong",
        status="partial",
        fetched=10,
        upserted=9,
        skipped=1,
        elapsed_sec=1.234,
        details={"api_total_count": 12},
        unknown_codes=Counter({("job_codes", "0013099"): 3}),
        notes=["전체 12건 중 10건만 수집되어 미수집 정책 삭제를 건너뜀"],
    )

    text = format_report(report)

    assert "[ontong] status=partial elapsed=1.2s" in text
    assert "fetched=10 upserted=9 pruned=0 skipped=1 duplicates=0" in text
    assert "api_total_count=12" in text
    assert "job_codes='0013099': 3" in text
    assert "note: 전체 12건 중 10건만" in text


# --- 미수집 행 삭제 판단 (순수 함수) ---


@pytest.mark.parametrize(
    ("total", "unique", "expected"),
    [(0, 0, False), (0, 5, False), (10, 9, False), (10, 10, True), (10, 11, True)],
)
def test_is_complete_fetch(total, unique, expected):
    assert is_complete_fetch(total, unique) is expected


@pytest.mark.parametrize(
    ("stale", "existing", "expected"),
    [
        (0, 0, True),
        (0, 3262, True),
        (1, 0, False),
        (1, 20, True),
        (2, 20, False),
        (163, 3262, True),
        (164, 3262, False),
        (3261, 3262, False),
    ],
)
def test_prune_allowed_caps_deletions_at_five_percent(stale, existing, expected):
    assert prune_allowed(stale, existing) is expected


def test_prune_result_blocked():
    assert PruneResult(existing=20, stale=2, deleted=0).blocked
    assert not PruneResult(existing=20, stale=1, deleted=1).blocked
    assert not PruneResult(existing=20, stale=0, deleted=0).blocked


# --- 실행 흐름 (가짜 엔진·클라이언트, loader 는 기록만) ---


class FakeEngine:
    @asynccontextmanager
    async def begin(self):
        yield object()


class Recorder:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, prune_result: PruneResult | None = None):
        self.finished: dict[str, Any] = {}
        self.prune_flags: list[tuple[str, bool]] = []
        self.prune_result = prune_result or PruneResult(existing=3, stale=0, deleted=0)

        async def start_run(engine, source):
            return 1

        async def finish_run(engine, run_id, **kwargs):
            self.finished = kwargs

        async def load_policies(conn, rows, *, prune):
            self.prune_flags.append(("policies", prune))
            return len(rows), self.prune_result if prune else None

        async def load_companies(conn, rows):
            return len(rows)

        async def load_saving_products(conn, grp, products, options, *, prune):
            self.prune_flags.append((grp, prune))
            return len(products), len(options), self.prune_result if prune else None

        for name, fn in {
            "start_run": start_run,
            "finish_run": finish_run,
            "load_policies": load_policies,
            "load_companies": load_companies,
            "load_saving_products": load_saving_products,
        }.items():
            monkeypatch.setattr(loader, name, fn)


class FakeOntong:
    def __init__(self, result: OntongFetchResult | BaseException):
        self.result = result

    async def fetch_all_policies(self) -> OntongFetchResult:
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _ontong_result(ids: list[str], total: int) -> OntongFetchResult:
    base = _ontong()[0]
    return OntongFetchResult(total_count=total, policies=[{**base, "plcyNo": i} for i in ids])


async def test_run_ontong_complete_fetch_prunes_and_succeeds(monkeypatch):
    rec = Recorder(monkeypatch, PruneResult(existing=3, stale=1, deleted=1))

    report = await run_ontong(FakeEngine(), FakeOntong(_ontong_result(["A", "B", "C"], 3)))

    assert rec.prune_flags == [("policies", True)]
    assert (report.status, report.pruned, report.upserted) == ("success", 1, 3)
    assert rec.finished["status"] == "success"


async def test_run_ontong_duplicates_do_not_count_toward_completeness(monkeypatch):
    rec = Recorder(monkeypatch)

    report = await run_ontong(FakeEngine(), FakeOntong(_ontong_result(["A", "B", "B"], 3)))

    assert rec.prune_flags == [("policies", False)]
    assert report.status == "partial"
    assert report.details["unique_fetched"] == 2
    assert any("불완전" in n for n in report.notes)


async def test_run_ontong_blocked_prune_is_partial(monkeypatch):
    Recorder(monkeypatch, PruneResult(existing=3262, stale=3000, deleted=0))

    report = await run_ontong(FakeEngine(), FakeOntong(_ontong_result(["A"], 1)))

    assert report.status == "partial"
    assert report.pruned == 0
    assert any("상한" in n for n in report.notes)


async def test_run_ontong_api_error_is_recorded_not_raised(monkeypatch):
    rec = Recorder(monkeypatch)

    report = await run_ontong(FakeEngine(), FakeOntong(IngestAPIError("온통청년 오류 응답")))

    assert report.status == "failed"
    assert rec.finished["status"] == "failed"
    assert "IngestAPIError" in rec.finished["error"]


@pytest.mark.parametrize("interrupt", [asyncio.CancelledError(), KeyboardInterrupt()])
async def test_run_interrupted_is_recorded_as_cancelled_then_reraised(monkeypatch, interrupt):
    rec = Recorder(monkeypatch)

    with pytest.raises(type(interrupt)):
        await run_ontong(FakeEngine(), FakeOntong(interrupt))

    assert rec.finished["status"] == "cancelled"
    assert rec.finished["error"]


class FakeFss:
    def __init__(self, listings: dict[str, tuple[FssListing, FssListing]]):
        self.listings = listings

    async def fetch_companies(self, grp: str) -> FssListing:
        return self.listings[grp][0]

    async def fetch_saving_products(self, grp: str) -> FssListing:
        return self.listings[grp][1]


def _fss_listings(grp: str, product_count: int, total: int) -> tuple[FssListing, FssListing]:
    base = _fss()["baseList"][0]
    companies = FssListing(grp, [{"fin_co_no": "C1", "kor_co_nm": "회사"}], [], 1)
    products = FssListing(
        grp,
        [{**base, "fin_prdt_cd": f"P{i}"} for i in range(product_count)],
        [],
        total,
    )
    return companies, products


async def test_run_fss_prunes_only_complete_groups(monkeypatch):
    rec = Recorder(monkeypatch)
    client = FakeFss(
        {"020000": _fss_listings("020000", 2, 2), "030300": _fss_listings("030300", 1, 3)}
    )

    report = await run_fss(FakeEngine(), client, fin_groups=("020000", "030300"))

    assert rec.prune_flags == [("020000", True), ("030300", False)]
    assert report.status == "partial"
    assert any("030300" in n and "불완전" in n for n in report.notes)


def test_pipeline_uses_loader_ratio_in_notes():
    report = SourceReport(source="x")
    ok = report.apply_prune("정책", True, PruneResult(existing=100, stale=10, deleted=0))
    assert not ok
    assert f"{loader.MAX_PRUNE_RATIO:.0%}" in report.notes[0]
    assert pipeline.is_complete_fetch is is_complete_fetch
