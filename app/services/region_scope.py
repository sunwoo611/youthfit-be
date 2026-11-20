"""전국 zip vs 기초지자체 가짜 전국 판정.

온통청년 zipCd 가 시군구 200개 이상으로 잡혀도, 기관명이 시·군·구 단위면
전국 사업으로 취급하지 않는다 (ADR 013).
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from sqlalchemy import ColumnElement, and_, func, not_

from app.ingest import codes
from app.models import Policy

# 시청·군청·구청, '군 '/'구 ' 표기, 또는 '…도 …시/군' (예: 경상북도 의성군).
# 시·군은 토큰 끝(공백·문자열 끝)에만 둔다. '지방시대정책국'처럼 중간 '시' 오탐 방지.
# '경기도청'처럼 광역만 있는 이름은 제외(도 뒤에 공백+시·군이 없음).
_BASIC_LOCAL_ORG_RE = re.compile(
    r"(?:시청|군청|구청|군 |구 )|(?:특별자치도|도)\s+\S*(?:시|군)(?:\s|$)"
)
BASIC_LOCAL_ORG_SQL = _BASIC_LOCAL_ORG_RE.pattern


def is_national_zip(sigungu_codes: Sequence[str] | None) -> bool:
    return len(sigungu_codes or []) >= codes.REGION_FREE_MIN_SIGUNGU


def is_basic_local_org(org_name: str | None) -> bool:
    if not org_name:
        return False
    return _BASIC_LOCAL_ORG_RE.search(org_name) is not None


def is_effective_national(
    sigungu_codes: Sequence[str] | None, org_name: str | None
) -> bool:
    """유효 전국 = 전국 zip 이고 기초지자체 기관이 아님."""
    return is_national_zip(sigungu_codes) and not is_basic_local_org(org_name)


def is_fake_national(sigungu_codes: Sequence[str] | None, org_name: str | None) -> bool:
    return is_national_zip(sigungu_codes) and is_basic_local_org(org_name)


def national_zip_expr() -> ColumnElement[bool]:
    return func.cardinality(Policy.sigungu_codes) >= codes.REGION_FREE_MIN_SIGUNGU


def basic_local_org_expr() -> ColumnElement[bool]:
    return and_(
        Policy.org_name.is_not(None),
        Policy.org_name.op("~")(BASIC_LOCAL_ORG_SQL),
    )


def effective_national_expr() -> ColumnElement[bool]:
    return and_(national_zip_expr(), not_(basic_local_org_expr()))


def fake_national_expr() -> ColumnElement[bool]:
    return and_(national_zip_expr(), basic_local_org_expr())


def org_matches_sido_expr(sido_code: str) -> ColumnElement[bool]:
    """기관명에 해당 시도 한글명이 포함되는지."""
    return Policy.org_name.contains(codes.SIDO[sido_code])
