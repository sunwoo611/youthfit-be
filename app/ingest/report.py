"""적재 데이터 필드 채움률. `uv run python -m app.ingest.report`.

DB 조회만 한다. 집계와 크롤링 판단은 순수 함수라 테스트가 DB 없이 돈다.
"""

import argparse
import asyncio
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.config import get_settings
from app.core.database import dispose_engine, get_engine
from app.ingest import codes
from app.ingest.normalize import KST
from app.models import FinancialCompany, Policy, SavingProduct
from app.services.policy_status import Availability, policy_availability

REGION_FREE_MIN_SIGUNGU = codes.REGION_FREE_MIN_SIGUNGU
HIGH_RATIO = 0.5
CORE_FIELDS_REQUIRED = 2
SHORT_SUPPORT_CHARS = 30
LONG_SUPPORT_CHARS = 100
INQUIRY_MARK = "별도 문의"

GROUP_ALL = "all"
GROUP_SEOUL_GYEONGGI = "seoul_gyeonggi"
GROUP_REGION_FREE = "region_free"
GROUP_ORDER = (GROUP_ALL, GROUP_SEOUL_GYEONGGI, GROUP_REGION_FREE)
GROUP_LABELS = {
    GROUP_ALL: "전체",
    GROUP_SEOUL_GYEONGGI: "서울·경기",
    GROUP_REGION_FREE: "지역 무관",
}
SEOUL_GYEONGGI_CODES = frozenset({"11", "41"})

_FLAG_FIELDS = (
    "apply_url_empty",
    "ref_urls_empty",
    "no_url",
    "apply_method_weak",
    "screening_method_weak",
    "documents_weak",
    "age_unlimited",
    "earn_unrestricted",
    "job_unrestricted",
    "apply_end_present",
    "open",
    "upcoming",
    "closed",
    "unverified",
    "support_empty",
    "support_short",
    "support_mid",
    "support_long",
)


@dataclass
class PolicyStats:
    total: int = 0
    apply_url_empty: int = 0
    ref_urls_empty: int = 0
    no_url: int = 0
    apply_method_weak: int = 0
    screening_method_weak: int = 0
    documents_weak: int = 0
    age_unlimited: int = 0
    earn_unrestricted: int = 0
    job_unrestricted: int = 0
    apply_end_present: int = 0
    open: int = 0
    upcoming: int = 0
    closed: int = 0
    unverified: int = 0
    support_empty: int = 0
    support_short: int = 0
    support_mid: int = 0
    support_long: int = 0

    def observe(self, flags: Mapping[str, bool]) -> None:
        self.total += 1
        for name in _FLAG_FIELDS:
            if flags[name]:
                setattr(self, name, getattr(self, name) + 1)


@dataclass(frozen=True)
class SavingStats:
    products: int
    youth: int
    homepage_mapped: int


@dataclass(frozen=True)
class CrawlJudgment:
    """supplement: 참고 URL 선별 보완 검토. api_only: 크롤링 없이 진행. unmeasured: 대상 0건."""

    code: str
    summary: str


def is_blank(value: object) -> bool:
    return not isinstance(value, str) or not value.strip()


def is_weak_text(value: object) -> bool:
    """공백이거나 본문에 '별도 문의'가 있으면 신청·심사·서류 안내로 쓰지 않는다."""
    if not isinstance(value, str):
        return True
    text = value.strip()
    return not text or INQUIRY_MARK in text


def policy_groups(
    sido_codes: list[str] | None, sigungu_codes: list[str] | None
) -> tuple[str, ...]:
    """한 정책이 집계되는 그룹. 전체는 항상 포함하고, 서울·경기와 지역 무관은 겹칠 수 있다."""
    sido = set(sido_codes or [])
    keys = [GROUP_ALL]
    if sido & SEOUL_GYEONGGI_CODES:
        keys.append(GROUP_SEOUL_GYEONGGI)
    if len(sigungu_codes or []) >= REGION_FREE_MIN_SIGUNGU:
        keys.append(GROUP_REGION_FREE)
    return tuple(keys)


def _ref_urls_empty(value: object) -> bool:
    if not isinstance(value, list):
        return True
    return not any(isinstance(url, str) and url.strip() for url in value)


def _support_bucket(value: object) -> str:
    if is_blank(value):
        return "empty"
    assert isinstance(value, str)
    length = len(value.strip())
    if length < SHORT_SUPPORT_CHARS:
        return "short"
    if length < LONG_SUPPORT_CHARS:
        return "mid"
    return "long"


def _as_date(value: object) -> date | None:
    return value if isinstance(value, date) else None


def _policy_flags(row: Mapping[str, object], today: date) -> dict[str, bool]:
    apply_url_empty = is_blank(row.get("apply_url"))
    ref_urls_empty = _ref_urls_empty(row.get("ref_urls"))
    bucket = _support_bucket(row.get("support_content"))
    apply_end = row.get("apply_end")
    job_codes = row.get("job_codes") or []
    code = row.get("apply_period_code")
    status = policy_availability(
        code if isinstance(code, str) else None,
        apply_start=_as_date(row.get("apply_start")),
        apply_end=_as_date(apply_end),
        biz_start=_as_date(row.get("biz_start")),
        biz_end=_as_date(row.get("biz_end")),
        today=today,
    )
    return {
        "apply_url_empty": apply_url_empty,
        "ref_urls_empty": ref_urls_empty,
        "no_url": apply_url_empty and ref_urls_empty,
        "apply_method_weak": is_weak_text(row.get("apply_method")),
        "screening_method_weak": is_weak_text(row.get("screening_method")),
        "documents_weak": is_weak_text(row.get("documents")),
        "age_unlimited": row.get("age_limited") is False,
        "earn_unrestricted": row.get("earn_cond_code") == codes.EARN_ANY,
        "job_unrestricted": not job_codes or codes.JOB_ANY in job_codes,
        "apply_end_present": isinstance(apply_end, date),
        "open": status is Availability.OPEN,
        "upcoming": status is Availability.UPCOMING,
        "closed": status is Availability.CLOSED,
        "unverified": status is Availability.UNVERIFIED,
        "support_empty": bucket == "empty",
        "support_short": bucket == "short",
        "support_mid": bucket == "mid",
        "support_long": bucket == "long",
    }


def aggregate_policies(rows: Iterable[Mapping[str, object]], today: date) -> dict[str, PolicyStats]:
    groups = {key: PolicyStats() for key in GROUP_ORDER}
    for row in rows:
        flags = _policy_flags(row, today)
        sido = row.get("sido_codes")
        sigungu = row.get("sigungu_codes")
        for key in policy_groups(
            list(sido) if isinstance(sido, list) else None,
            list(sigungu) if isinstance(sigungu, list) else None,
        ):
            groups[key].observe(flags)
    return groups


def aggregate_savings(rows: Iterable[Mapping[str, object]]) -> SavingStats:
    products = youth = homepage_mapped = 0
    for row in rows:
        products += 1
        if row.get("is_youth"):
            youth += 1
        if not is_blank(row.get("homepage_url")):
            homepage_mapped += 1
    return SavingStats(products=products, youth=youth, homepage_mapped=homepage_mapped)


def _meets(part: int, total: int) -> bool:
    return total > 0 and part / total >= HIGH_RATIO


def format_ratio(part: int, total: int) -> str:
    if total == 0:
        return "—"
    return f"{part / total * 100:.1f}% ({part}/{total})"


def judge_crawl(seoul: PolicyStats) -> CrawlJudgment:
    """서울·경기에서 URL 전무와 핵심 안내 빈약이 함께 높을 때만 선별 보완을 권한다."""
    if seoul.total == 0:
        return CrawlJudgment(
            "unmeasured",
            "서울·경기 정책이 0건이라 크롤링 필요성을 판단하지 않았다.",
        )

    url_text = format_ratio(seoul.no_url, seoul.total)
    core = (
        ("신청 방법", seoul.apply_method_weak),
        ("심사 방법", seoul.screening_method_weak),
        ("제출 서류", seoul.documents_weak),
    )
    core_high = [name for name, part in core if _meets(part, seoul.total)]
    if _meets(seoul.no_url, seoul.total) and len(core_high) >= CORE_FIELDS_REQUIRED:
        fields = ", ".join(core_high)
        return CrawlJudgment(
            "supplement",
            f"서울·경기 {seoul.total}건 중 URL이 하나도 없는 비율은 {url_text}이고, "
            f"핵심 안내 {len(core_high)}개 필드({fields})가 기준(50%) 이상 비어 있다. "
            "서울·경기 핵심 정책의 참고 URL 선별 보완을 검토한다.",
        )

    detail = ", ".join(f"{name} {format_ratio(part, seoul.total)}" for name, part in core)
    return CrawlJudgment(
        "api_only",
        f"서울·경기 {seoul.total}건 중 URL이 하나도 없는 비율은 {url_text}이다. "
        f"핵심 안내는 {detail}. "
        "URL 부재와 핵심 안내 빈약이 동시에 기준(50%)을 넘지 않아 "
        "크롤링 없이 API 데이터로 진행한다.",
    )


def _metric_row(label: str, stats: dict[str, PolicyStats], attr: str) -> str:
    cells = " | ".join(format_ratio(getattr(item, attr), item.total) for item in stats.values())
    return f"| {label} | {cells} |"


def render_markdown(
    policies: dict[str, PolicyStats], savings: SavingStats, today: date
) -> str:
    ordered = {key: policies[key] for key in GROUP_ORDER}
    headers = " | ".join(GROUP_LABELS[key] for key in GROUP_ORDER)
    counts = " | ".join(str(ordered[key].total) for key in GROUP_ORDER)
    ratio_rows = "\n".join(
        _metric_row(label, ordered, attr)
        for label, attr in (
            ("신청 URL 없음", "apply_url_empty"),
            ("참고 URL 없음", "ref_urls_empty"),
            ("URL 하나도 없음", "no_url"),
            ("신청 방법 별도 문의·빈 값", "apply_method_weak"),
            ("심사 방법 별도 문의·빈 값", "screening_method_weak"),
            ("제출 서류 별도 문의·빈 값", "documents_weak"),
            ("나이 제한 없음", "age_unlimited"),
            ("소득 조건 무관", "earn_unrestricted"),
            ("취업상태 제한없음", "job_unrestricted"),
            ("신청 종료일 있음", "apply_end_present"),
            ("신청 가능", "open"),
            ("신청 예정", "upcoming"),
            ("마감", "closed"),
            ("신청기간 확인 필요", "unverified"),
            ("지원 내용 없음", "support_empty"),
            (f"지원 내용 짧음 (1~{SHORT_SUPPORT_CHARS - 1}자)", "support_short"),
            (f"지원 내용 {SHORT_SUPPORT_CHARS}~{LONG_SUPPORT_CHARS - 1}자", "support_mid"),
            (f"지원 내용 {LONG_SUPPORT_CHARS}자 이상", "support_long"),
        )
    )
    judgment = judge_crawl(ordered[GROUP_SEOUL_GYEONGGI])
    brief_cells = " | ".join(
        format_ratio(item.support_empty + item.support_short, item.total)
        for item in ordered.values()
    )
    live_unverified_cells = " | ".join(
        format_ratio(item.unverified, item.total - item.closed) for item in ordered.values()
    )
    return "\n".join(
        [
            "# 적재 데이터 품질",
            "",
            f"- 기준일: {today.isoformat()} (Asia/Seoul).",
            "- 마감: 신청 종료일이나 사업 종료일이 기준일보다 이전. "
            "사업기간 문구는 계속 사업 표현이 없을 때만 마지막 연도 12월 31일을 종료일로 본다.",
            "- 신청 가능: 특정 기간은 신청 기간 안, 상시는 사업 종료 전. "
            "신청기간 확인 필요: 신청기간 미지정이고 사업도 끝나지 않은 정책.",
            "- 서울·경기: 시도 코드 11 또는 41을 포함.",
            f"- 지역 무관: 시군구 코드 {REGION_FREE_MIN_SIGUNGU}개 이상. "
            "단일 시도는 시군구 48개 이하이고, 전국 정책은 242~256개에 몰려 있다.",
            "- 그룹은 서로 겹칠 수 있다. "
            "전국 정책이 11·41을 포함하면 서울·경기와 지역 무관에 함께 센다.",
            f"- 별도 문의·빈 값: 공백이거나 본문에 '{INQUIRY_MARK}'가 있는 경우.",
            f"- 소득 조건 무관은 코드 {codes.EARN_ANY}, "
            f"취업상태 제한없음은 코드가 없거나 {codes.JOB_ANY}.",
            "",
            "## 정책",
            "",
            f"| 지표 | {headers} |",
            "|---|---:|---:|---:|",
            f"| 정책 수 | {counts} |",
            ratio_rows,
            f"| 지원 내용 짧거나 없음 ({SHORT_SUPPORT_CHARS}자 미만) | {brief_cells} |",
            f"| 마감 제외 후보 중 신청기간 확인 필요 | {live_unverified_cells} |",
            "",
            "## 적금",
            "",
            "| 지표 | 값 |",
            "|---|---:|",
            f"| 상품 수 | {savings.products} |",
            f"| 청년 상품 | {format_ratio(savings.youth, savings.products)} |",
            "| 금융회사 홈페이지 URL 매핑 | "
            f"{format_ratio(savings.homepage_mapped, savings.products)} |",
            "",
            "## 크롤링 필요성",
            "",
            "판단은 서울·경기 열만 본다.",
            "",
            f"- URL이 하나도 없는 비율이 {HIGH_RATIO:.0%} 이상이고",
            "- 신청 방법·심사 방법·제출 서류 중 "
            f"{CORE_FIELDS_REQUIRED}개 이상이 별도 문의·빈 값 비율 {HIGH_RATIO:.0%} 이상",
            "",
            "이면 서울·경기 핵심 정책의 참고 URL 선별 보완을 검토한다. "
            "하나라도 낮으면 크롤링 없이 API 데이터로 진행한다. "
            "서울·경기 정책이 0건이면 판단하지 않는다.",
            "",
            f"결론: {judgment.summary}",
            "",
        ]
    )


def _policy_select():
    return select(
        Policy.sido_codes,
        Policy.sigungu_codes,
        Policy.apply_url,
        Policy.ref_urls,
        Policy.apply_method,
        Policy.screening_method,
        Policy.documents,
        Policy.age_limited,
        Policy.earn_cond_code,
        Policy.job_codes,
        Policy.apply_period_code,
        Policy.apply_start,
        Policy.apply_end,
        Policy.biz_start,
        Policy.biz_end,
        Policy.support_content,
    )


def _saving_select():
    return (
        select(SavingProduct.is_youth, FinancialCompany.homepage_url)
        .select_from(SavingProduct)
        .outerjoin(FinancialCompany, FinancialCompany.fin_co_no == SavingProduct.fin_co_no)
    )


async def load_report(
    conn: AsyncConnection, today: date | None = None
) -> tuple[dict[str, PolicyStats], SavingStats, date]:
    """policies·saving_products 를 읽어 집계한다. 쓰기는 하지 않는다."""
    as_of = today or datetime.now(KST).date()
    policy_rows = (await conn.execute(_policy_select())).mappings().all()
    saving_rows = (await conn.execute(_saving_select())).mappings().all()
    return aggregate_policies(policy_rows, as_of), aggregate_savings(saving_rows), as_of


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.ingest.report",
        description="적재된 정책·적금의 필드 채움률을 조회한다. DB를 변경하지 않는다.",
    )
    parser.add_argument(
        "--today",
        type=date.fromisoformat,
        default=None,
        help="마감 판단 기준일 YYYY-MM-DD. 기본값은 한국 날짜.",
    )
    return parser.parse_args(argv)


async def main(today: date | None) -> int:
    settings = get_settings()
    engine = get_engine(settings)
    if engine is None:
        print(".env 에 DATABASE_URL 을 설정하세요", file=sys.stderr)
        return 2
    try:
        async with engine.connect() as conn:
            policies, savings, as_of = await load_report(conn, today)
    except (OSError, SQLAlchemyError) as exc:
        print(f"DB 조회 실패: {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        await dispose_engine()
    print(render_markdown(policies, savings, as_of))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(parse_args().today)))
