import json
from collections import Counter
from pathlib import Path

from app.ingest.fss import FssListing
from app.ingest.pipeline import (
    SourceReport,
    format_report,
    prepare_companies,
    prepare_policies,
    prepare_saving,
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
