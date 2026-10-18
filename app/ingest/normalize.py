"""공공 API 원문 → DB 컬럼 dict 변환. DB·네트워크에 의존하지 않는 순수 함수만 둔다.

반환 dict 의 키는 ORM 모델 속성명과 같고, 원문은 `raw` 에 그대로 담는다.
"""

import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

from app.ingest import codes

KST = timezone(timedelta(hours=9))

# 의미 없는 값. 품질 측정을 위해 "별도 문의" 같은 안내 문구는 남긴다.
_PLACEHOLDERS = frozenset({"-", "--", "null", "NULL"})

_DATE_RANGE = re.compile(r"(\d{8})\s*~\s*(\d{8})")
_POLICY_CODE = re.compile(r"^\d{7}$")
_SIGUNGU_CODE = re.compile(r"^\d{5}$")

# Policy 코드 컬럼 → 원문 필드 (앞쪽 우선).
# 특화 필드는 문서(sBizCd)와 실제 응답(sbizCd) 표기가 다르다.
_POLICY_CODE_FIELDS: dict[str, tuple[str, ...]] = {
    "marriage_code": ("mrgSttsCd",),
    "earn_cond_code": ("earnCndSeCd",),
    "job_codes": ("jobCd",),
    "school_codes": ("schoolCd",),
    "major_codes": ("plcyMajorCd",),
    "special_codes": ("sbizCd", "sBizCd"),
    "apply_period_code": ("aplyPrdSeCd",),
}
_SINGLE_CODE_COLUMNS = frozenset({"marriage_code", "earn_cond_code", "apply_period_code"})

YOUTH_KEYWORDS = ("청년", "사회초년생")
# 예: "만 19세 이상 ~ 만 34세 이하", "만19~34세"
_YOUTH_AGE = re.compile(r"(?:1[5-9]|2\d)\s*세?\s*(?:이상)?\s*~\s*(?:만\s*)?(?:29|3\d)\s*세")


class NormalizeError(ValueError):
    """필수 필드가 없어 적재할 수 없는 레코드."""


def clean_text(value: Any) -> str | None:
    """공백만 있는 값(예: 날짜 자리 공백 8칸)과 placeholder 는 None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in _PLACEHOLDERS:
        return None
    return text


def split_csv(value: Any) -> list[str]:
    """콤마 목록 → 공백 제거·중복 제거(순서 유지)."""
    text = clean_text(value)
    if text is None:
        return []
    return list(dict.fromkeys(part.strip() for part in text.split(",") if part.strip()))


def parse_int(value: Any) -> int | None:
    text = clean_text(value)
    if text is None:
        return None
    try:
        return int(text.replace(",", ""))
    except ValueError:
        return None


def parse_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value).strip())
    except InvalidOperation:
        return None


def parse_yyyymmdd(value: Any) -> date | None:
    text = clean_text(value)
    if text is None or len(text) != 8 or not text.isdigit():
        return None
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        return None


def parse_apply_period(value: Any) -> tuple[date | None, date | None]:
    """aplyYmd("20260518 ~ 20260630") → (시작, 종료).

    여러 구간이면 가장 이른 시작·가장 늦은 종료를 쓴다.
    """
    text = clean_text(value)
    if text is None:
        return None, None
    starts: list[date] = []
    ends: list[date] = []
    for start_raw, end_raw in _DATE_RANGE.findall(text):
        start, end = parse_yyyymmdd(start_raw), parse_yyyymmdd(end_raw)
        if start:
            starts.append(start)
        if end:
            ends.append(end)
    return (min(starts) if starts else None, max(ends) if ends else None)


def parse_kst_datetime(value: Any) -> datetime | None:
    text = clean_text(value)
    if text is None:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
    except ValueError:
        return None


def _http_url(candidate: str) -> str | None:
    if any(ch.isspace() for ch in candidate):
        return None
    if "://" not in candidate:
        candidate = f"http://{candidate}"
    try:
        parts = urlsplit(candidate)
        host = parts.hostname
        parts.port  # noqa: B018 - 잘못된 포트면 ValueError
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    labels = host.split(".")
    tld = labels[-1]
    if len(labels) < 2 or not all(labels) or len(tld) < 2 or not tld.isalpha():
        return None
    return candidate


def clean_url(value: Any) -> str | None:
    """http(s) URL 만 통과. 스킴 없는 도메인은 http:// 를 붙인다.

    호스트는 알파벳 최상위 도메인으로 끝나야 한다(날짜·버전 문자열 차단).
    콤마로 여러 URL 이 붙어 있으면 첫 번째 유효한 URL 을 쓴다.
    """
    text = clean_text(value)
    if text is None:
        return None
    for candidate in text.split(","):
        url = _http_url(candidate.strip())
        if url:
            return url
    return None


def _raw_field(raw: dict[str, Any], keys: tuple[str, ...]) -> Any:
    return next((raw[k] for k in keys if clean_text(raw.get(k)) is not None), None)


def _codes(value: Any) -> list[str]:
    return [c for c in split_csv(value) if _POLICY_CODE.match(c)]


def _code(value: Any) -> str | None:
    text = clean_text(value)
    return text if text and _POLICY_CODE.match(text) else None


def parse_zip_codes(value: Any) -> tuple[list[str], list[str]]:
    """zipCd(법정 시군구 5자리 콤마 목록) → (시군구 코드, 앞 2자리 시도 코드)."""
    sigungu = [c for c in split_csv(value) if _SIGUNGU_CODE.match(c)]
    sido = sorted({c[:2] for c in sigungu})
    return sigungu, sido


def normalize_policy(raw: dict[str, Any]) -> dict[str, Any]:
    plcy_no = clean_text(raw.get("plcyNo"))
    name = clean_text(raw.get("plcyNm"))
    if plcy_no is None or name is None:
        raise NormalizeError(f"plcyNo/plcyNm 누락: plcyNo={plcy_no!r}")

    age_limited = clean_text(raw.get("sprtTrgtAgeLmtYn")) != codes.AGE_NO_LIMIT_FLAG
    min_age = parse_int(raw.get("sprtTrgtMinAge")) if age_limited else None
    max_age = parse_int(raw.get("sprtTrgtMaxAge")) if age_limited else None

    earn_cond_code = _code(raw.get("earnCndSeCd"))
    is_range = earn_cond_code == codes.EARN_RANGE

    sigungu_codes, sido_codes = parse_zip_codes(raw.get("zipCd"))
    apply_start, apply_end = parse_apply_period(raw.get("aplyYmd"))
    ref_urls = [
        url
        for url in dict.fromkeys(clean_url(raw.get(k)) for k in ("refUrlAddr1", "refUrlAddr2"))
        if url
    ]

    return {
        "plcy_no": plcy_no,
        "name": name,
        "keywords": split_csv(raw.get("plcyKywdNm")),
        "description": clean_text(raw.get("plcyExplnCn")),
        "categories": split_csv(raw.get("lclsfNm")),
        "subcategories": split_csv(raw.get("mclsfNm")),
        "support_content": clean_text(raw.get("plcySprtCn")),
        "min_age": min_age or None,
        "max_age": max_age or None,
        "age_limited": age_limited,
        "marriage_code": _code(raw.get("mrgSttsCd")),
        "earn_cond_code": earn_cond_code,
        "earn_min": parse_int(raw.get("earnMinAmt")) if is_range else None,
        "earn_max": parse_int(raw.get("earnMaxAmt")) if is_range else None,
        "earn_etc": clean_text(raw.get("earnEtcCn")),
        "job_codes": _codes(raw.get("jobCd")),
        "school_codes": _codes(raw.get("schoolCd")),
        "special_codes": _codes(_raw_field(raw, _POLICY_CODE_FIELDS["special_codes"])),
        "major_codes": _codes(raw.get("plcyMajorCd")),
        "sigungu_codes": sigungu_codes,
        "sido_codes": sido_codes,
        "apply_period_code": _code(raw.get("aplyPrdSeCd")),
        "apply_start": apply_start,
        "apply_end": apply_end,
        "apply_url": clean_url(raw.get("aplyUrlAddr")),
        "ref_urls": ref_urls,
        "apply_method": clean_text(raw.get("plcyAplyMthdCn")),
        "screening_method": clean_text(raw.get("srngMthdCn")),
        "documents": clean_text(raw.get("sbmsnDcmntCn")),
        "extra_qualification": clean_text(raw.get("addAplyQlfcCndCn")),
        "exclusion": clean_text(raw.get("ptcpPrpTrgtCn")),
        "org_name": clean_text(raw.get("sprvsnInstCdNm")) or clean_text(raw.get("operInstCdNm")),
        "source_modified_at": parse_kst_datetime(raw.get("lastMdfcnDt")),
        "raw": raw,
    }


def find_unknown_codes(raw: dict[str, Any]) -> list[tuple[str, str]]:
    """원문 정책에서 코드표에 없는 (컬럼, 값) 목록. 코드표 변경·형식 오류 감지용.

    정규화는 형식이 깨진 코드를 버리므로, 버려지는 값까지 세려면 원문을 봐야 한다.
    단일 코드 컬럼에 콤마로 여러 값이 오면 전체 문자열을 하나의 미등록 값으로 본다.
    """
    unknown: list[tuple[str, str]] = []
    for column, keys in _POLICY_CODE_FIELDS.items():
        table = codes.POLICY_CODE_COLUMNS[column]
        value = _raw_field(raw, keys)
        if column in _SINGLE_CODE_COLUMNS:
            text = clean_text(value)
            values = [text] if text else []
        else:
            values = split_csv(value)
        unknown.extend((column, v) for v in values if v not in table)
    return unknown


def is_youth_product(raw: dict[str, Any]) -> bool:
    """상품명·가입대상·우대조건·기타안내 텍스트에 청년 키워드나 청년 연령대 표기가 있으면 True."""
    text = " ".join(
        clean_text(raw.get(k)) or "" for k in ("fin_prdt_nm", "join_member", "spcl_cnd", "etc_note")
    )
    return any(k in text for k in YOUTH_KEYWORDS) or bool(_YOUTH_AGE.search(text))


def _required(raw: dict[str, Any], *keys: str) -> dict[str, str]:
    values = {k: clean_text(raw.get(k)) for k in keys}
    missing = [k for k, v in values.items() if v is None]
    if missing:
        raise NormalizeError(f"필수 필드 누락: {', '.join(missing)}")
    return values  # type: ignore[return-value]


def normalize_company(raw: dict[str, Any], fin_grp_code: str) -> dict[str, Any]:
    req = _required(raw, "fin_co_no", "kor_co_nm")
    return {
        "fin_co_no": req["fin_co_no"],
        "fin_grp_code": fin_grp_code,
        "name": req["kor_co_nm"],
        "homepage_url": clean_url(raw.get("homp_url")),
        "call_center": clean_text(raw.get("cal_tel")),
        "raw": raw,
    }


def normalize_saving_product(raw: dict[str, Any], fin_grp_code: str) -> dict[str, Any]:
    req = _required(raw, "fin_co_no", "fin_prdt_cd", "fin_prdt_nm")
    return {
        "fin_co_no": req["fin_co_no"],
        "fin_prdt_cd": req["fin_prdt_cd"],
        "fin_grp_code": fin_grp_code,
        "company_name": clean_text(raw.get("kor_co_nm")),
        "name": req["fin_prdt_nm"],
        "join_way": clean_text(raw.get("join_way")),
        "special_condition": clean_text(raw.get("spcl_cnd")),
        "join_deny": clean_text(raw.get("join_deny")),
        "join_member": clean_text(raw.get("join_member")),
        "etc_note": clean_text(raw.get("etc_note")),
        "max_limit": parse_int(raw.get("max_limit")),
        "dcls_month": clean_text(raw.get("dcls_month")),
        "is_youth": is_youth_product(raw),
        "raw": raw,
    }


def normalize_saving_option(raw: dict[str, Any]) -> dict[str, Any]:
    req = _required(raw, "fin_co_no", "fin_prdt_cd", "intr_rate_type", "rsrv_type")
    term_months = parse_int(raw.get("save_trm"))
    if term_months is None:
        raise NormalizeError(f"save_trm 누락: {req['fin_prdt_cd']}")
    return {
        "fin_co_no": req["fin_co_no"],
        "fin_prdt_cd": req["fin_prdt_cd"],
        "rate_type": req["intr_rate_type"],
        "rate_type_name": clean_text(raw.get("intr_rate_type_nm")),
        "reserve_type": req["rsrv_type"],
        "reserve_type_name": clean_text(raw.get("rsrv_type_nm")),
        "term_months": term_months,
        "base_rate": parse_decimal(raw.get("intr_rate")),
        "max_rate": parse_decimal(raw.get("intr_rate2")),
    }
