import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.ingest import codes
from app.ingest.normalize import (
    KST,
    NormalizeError,
    clean_text,
    clean_url,
    find_unknown_codes,
    is_youth_product,
    normalize_company,
    normalize_policy,
    normalize_saving_option,
    normalize_saving_product,
    parse_apply_period,
    parse_biz_period,
    parse_zip_codes,
)
from app.models import FinancialCompany, Policy, SavingProduct, SavingProductOption

SAMPLES = Path(__file__).resolve().parents[1] / "docs" / "data-samples"


def _load(name: str) -> dict:
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def ontong_policies() -> list[dict]:
    return _load("ontong-getPlcy-sample.json")["result"]["youthPolicyList"]


@pytest.fixture(scope="module")
def fss_result() -> dict:
    return _load("fss-savingProductsSearch-sample.json")["result"]


def _policy_by_no(policies: list[dict], suffix: str) -> dict:
    return next(p for p in policies if p["plcyNo"].endswith(suffix))


@pytest.mark.parametrize(
    ("value", "expected"),
    [("        ", None), ("", None), (None, None), ("-", None), (" 별도 문의 ", "별도 문의")],
)
def test_clean_text(value, expected):
    assert clean_text(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            "https://youth.chungnam.go.kr/web/main/main",
            "https://youth.chungnam.go.kr/web/main/main",
        ),
        ("www.seoul.go.kr", "http://www.seoul.go.kr"),
        ("youth.seoul.go.kr/site/main", "http://youth.seoul.go.kr/site/main"),
        ("HTTPS://WWW.GOV.KR/portal", "HTTPS://WWW.GOV.KR/portal"),
        ("https://www.work24.go.kr:8443/a?b=1", "https://www.work24.go.kr:8443/a?b=1"),
        ("https://a.kr,https://b.kr", "https://a.kr"),
        ("별도 문의, www.b.kr", "http://www.b.kr"),
        ("", None),
        ("홈페이지 참조", None),
        ("https://a.kr 참조", None),
        ("2026.05.18", None),
        ("v1.0", None),
        ("http://", None),
        ("https://localhost/apply", None),
        ("https://a.kr:99999", None),
        ("mailto:help@youth.go.kr", None),
        ("javascript:alert(1)", None),
        ("ftp://files.go.kr", None),
    ],
)
def test_clean_url(value, expected):
    assert clean_url(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("20260518 ~ 20260630", (date(2026, 5, 18), date(2026, 6, 30))),
        ("20260101~20260131\\N20260301 ~ 20260331", (date(2026, 1, 1), date(2026, 3, 31))),
        ("", (None, None)),
        ("        ", (None, None)),
        ("상시 모집", (None, None)),
        ("20261340 ~ 20261231", (None, date(2026, 12, 31))),
        ("00010101 ~ 00010101", (None, None)),
        ("00020220 ~ 20260308", (None, date(2026, 3, 8))),
        ("20260101 ~ 21260101", (date(2026, 1, 1), None)),
    ],
)
def test_parse_apply_period(value, expected):
    assert parse_apply_period(value) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2025.1.~2025.12.", date(2025, 12, 31)),
        ("2025. 1. ~ 12.", date(2025, 12, 31)),
        ("2026-01-01~2026-12-31", date(2026, 12, 31)),
        ("2022. 3. ∼ 2027. 2.", date(2027, 12, 31)),
        ("'25.9월 중 2일간/미정", date(2025, 12, 31)),
        ("2025년 12월 31일", date(2025, 12, 31)),
        ("2026-01-01~2999-12-31", date(2026, 12, 31)),
        # 계속 진행되는 사업은 연도가 있어도 종료일을 두지 않는다
        ("상시", None),
        ("2018년 ~ 계속", None),
        ("'22년 ~ 게", None),
        ("'17~계", None),
        ("'15년~", None),
        ("2025. 1. ~", None),
        ("2023. 7. ~ 현재", None),
        ("2025. 1. ~ 예산소진시", None),
        ("'25.7월 중(매년 1회, 연례적 시행)", None),
        ("ㅇ 운영기간(연2회) - 동계 : 1,2월 중 ※ 2025 동계", None),
        ("2024년 이후 신규모집 종료", None),
        ("‘26. 하반기(초가을 경)", None),
        # 연도가 없거나 형식이 깨진 문구
        ("담당자 문의", None),
        ("20265. 1. ~ 12.", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_biz_period_text_end(text, expected):
    assert parse_biz_period({"bizPrdEtcCn": text}) == (None, expected)


def test_parse_biz_period_prefers_date_fields():
    raw = {
        "bizPrdBgngYmd": "20260420",
        "bizPrdEndYmd": "20260710",
        "bizPrdEtcCn": "2025.1.~2025.12.",
    }
    assert parse_biz_period(raw) == (date(2026, 4, 20), date(2026, 7, 10))


def test_parse_biz_period_blank_date_falls_back_to_text():
    raw = {"bizPrdBgngYmd": "        ", "bizPrdEndYmd": "        ", "bizPrdEtcCn": "2025년"}
    assert parse_biz_period(raw) == (None, date(2025, 12, 31))


def test_parse_biz_period_out_of_range_end_is_open():
    assert parse_biz_period({"bizPrdBgngYmd": "20260101", "bizPrdEndYmd": "29991231"}) == (
        date(2026, 1, 1),
        None,
    )


def test_parse_zip_codes_derives_sido():
    sigungu, sido = parse_zip_codes("11680, 11650,41135,11680,abc")
    assert sigungu == ["11680", "11650", "41135"]
    assert sido == ["11", "41"]


def test_normalize_policy_sample_fixed_period(ontong_policies):
    raw = _policy_by_no(ontong_policies, "113966")
    row = normalize_policy(raw)

    assert row["plcy_no"] == "20261016005400113966"
    assert row["name"] == "공공기관 청년고용의무제 운영"
    assert row["keywords"] == ["인턴"]
    assert row["categories"] == ["일자리"]
    assert row["subcategories"] == ["취업"]
    assert row["apply_period_code"] == codes.APPLY_PERIOD_FIXED
    assert (row["apply_start"], row["apply_end"]) == (date(2026, 5, 18), date(2026, 6, 30))
    assert (row["biz_start"], row["biz_end"]) == (date(2026, 4, 20), date(2026, 7, 10))
    assert row["job_codes"] == ["0013003"]
    assert row["marriage_code"] == codes.MARRIAGE_ANY
    assert row["earn_cond_code"] == codes.EARN_ANY
    assert row["earn_min"] is None and row["earn_max"] is None
    assert row["sido_codes"] == ["44"]
    assert len(row["sigungu_codes"]) == 13
    assert row["apply_url"] is None
    assert row["ref_urls"] == []
    assert row["apply_method"] == "별도 문의"
    assert row["org_name"] == "예산담당관"
    assert row["source_modified_at"] == datetime(2026, 10, 16, 17, 30, 39, tzinfo=KST)
    assert row["raw"] is raw


def test_normalize_policy_age_flag_y_means_no_limit(ontong_policies):
    """공식 화면은 sprtTrgtAgeLmtYn='Y' 를 '제한없음'으로 표시한다. 나이 값이 있어도 무시."""
    row = normalize_policy(_policy_by_no(ontong_policies, "113966"))
    assert row["age_limited"] is False
    assert row["min_age"] is None and row["max_age"] is None

    limited = normalize_policy(
        {**_policy_by_no(ontong_policies, "113966"), "sprtTrgtAgeLmtYn": "N"}
    )
    assert limited["age_limited"] is True
    assert (limited["min_age"], limited["max_age"]) == (19, 34)


def test_normalize_policy_blank_dates_and_etc_income(ontong_policies):
    row = normalize_policy(_policy_by_no(ontong_policies, "113967"))
    assert row["apply_period_code"] == codes.APPLY_PERIOD_ALWAYS
    assert (row["apply_start"], row["apply_end"]) == (None, None)
    assert (row["biz_start"], row["biz_end"]) == (None, date(2026, 12, 31))
    assert row["ref_urls"] == ["https://youth.chungnam.go.kr/web/main/main"]

    etc = normalize_policy(_policy_by_no(ontong_policies, "113965"))
    assert etc["earn_cond_code"] == "0043003"
    assert etc["earn_etc"] is None
    assert etc["screening_method"] == "부서 자체평가 후 개별 통보"


def test_normalize_policy_income_range():
    row = normalize_policy(
        {
            "plcyNo": "P1",
            "plcyNm": "테스트",
            "earnCndSeCd": codes.EARN_RANGE,
            "earnMinAmt": "0",
            "earnMaxAmt": "5,000",
            "lclsfNm": "주거,금융･복지･문화",
        }
    )
    assert (row["earn_min"], row["earn_max"]) == (0, 5000)
    assert row["categories"] == ["주거", "금융･복지･문화"]


def test_normalize_policy_requires_id_and_name():
    with pytest.raises(NormalizeError):
        normalize_policy({"plcyNo": "  ", "plcyNm": "이름"})


def test_normalized_policy_keys_match_model(ontong_policies):
    row = normalize_policy(ontong_policies[0])
    columns = {c.key for c in Policy.__table__.columns} - {"ingested_at"}
    assert set(row) == columns


def test_find_unknown_codes(ontong_policies):
    assert all(find_unknown_codes(p) == [] for p in ontong_policies)
    assert find_unknown_codes({**ontong_policies[0], "aplyPrdSeCd": "0057003"}) == []

    raw = {**ontong_policies[0], "jobCd": "0013003,0013099", "mrgSttsCd": "0055009"}
    assert sorted(find_unknown_codes(raw)) == [
        ("job_codes", "0013099"),
        ("marriage_code", "0055009"),
    ]


def test_find_unknown_codes_counts_malformed_values_dropped_by_normalize(ontong_policies):
    raw = {
        **ontong_policies[0],
        "jobCd": "13003,ABC",
        "mrgSttsCd": "0055001,0055002",
        "sbizCd": "",
        "sBizCd": "0014099",
    }
    row = normalize_policy(raw)
    assert row["job_codes"] == []
    assert row["marriage_code"] is None
    assert row["special_codes"] == ["0014099"]

    assert sorted(find_unknown_codes(raw)) == [
        ("job_codes", "13003"),
        ("job_codes", "ABC"),
        ("marriage_code", "0055001,0055002"),
        ("special_codes", "0014099"),
    ]


def test_normalize_saving_product_sample(fss_result):
    raw = fss_result["baseList"][0]
    row = normalize_saving_product(raw, "020000")

    assert (row["fin_co_no"], row["fin_prdt_cd"]) == ("0010001", "WR0001F")
    assert row["fin_grp_code"] == "020000"
    assert row["company_name"] == "우리은행"
    assert row["name"] == "우리SUPER주거래적금"
    assert row["max_limit"] is None
    assert row["dcls_month"] == "202609"
    assert row["is_youth"] is False
    columns = {c.key for c in SavingProduct.__table__.columns} - {"ingested_at"}
    assert set(row) == columns


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"fin_prdt_nm": "KB청년도약적금"}, True),
        ({"join_member": "만 19세 이상 ~ 만 34세 이하 실명의 개인"}, True),
        ({"join_member": "만19~34세 개인"}, True),
        ({"spcl_cnd": "사회초년생 우대 0.5%p"}, True),
        ({"join_member": "실명의 개인", "etc_note": "가입기간 12~36개월"}, False),
        ({"join_member": "만 65세 이상 개인"}, False),
    ],
)
def test_is_youth_product(fields, expected):
    assert is_youth_product(fields) is expected


def test_normalize_saving_option_sample(fss_result):
    row = normalize_saving_option(fss_result["optionList"][0])
    assert row == {
        "fin_co_no": "0010001",
        "fin_prdt_cd": "WR0001F",
        "rate_type": "S",
        "rate_type_name": "단리",
        "reserve_type": "F",
        "reserve_type_name": "자유적립식",
        "term_months": 12,
        "base_rate": Decimal("2.45"),
        "max_rate": Decimal("3.85"),
    }
    columns = {c.key for c in SavingProductOption.__table__.columns} - {"id"}
    assert set(row) == columns


def test_normalize_saving_option_null_rate(fss_result):
    row = normalize_saving_option({**fss_result["optionList"][0], "intr_rate2": None})
    assert row["max_rate"] is None


def test_normalize_company():
    raw = {
        "fin_co_no": "0010001",
        "kor_co_nm": "우리은행",
        "homp_url": "https://www.wooribank.com",
        "cal_tel": "1588-5000",
    }
    row = normalize_company(raw, "020000")
    assert row["homepage_url"] == "https://www.wooribank.com"
    assert row["call_center"] == "1588-5000"
    columns = {c.key for c in FinancialCompany.__table__.columns} - {"ingested_at"}
    assert set(row) == columns


def test_normalize_saving_requires_keys():
    with pytest.raises(NormalizeError, match="fin_prdt_cd"):
        normalize_saving_product({"fin_co_no": "1", "fin_prdt_nm": "x"}, "020000")
