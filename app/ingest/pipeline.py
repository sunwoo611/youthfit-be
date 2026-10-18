"""수집 → 정규화 → 적재 흐름과 소스별 실행 결과.

외부 API 호출은 DB 트랜잭션 밖에서 끝내고, 적재는 소스별 단일 트랜잭션으로 한다.
"""

import logging
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from app.ingest import loader
from app.ingest.fss import DEFAULT_FIN_GROUPS, FssClient, FssListing
from app.ingest.normalize import (
    NormalizeError,
    find_unknown_codes,
    normalize_company,
    normalize_policy,
    normalize_saving_option,
    normalize_saving_product,
)
from app.ingest.ontong import OntongClient

logger = logging.getLogger(__name__)

MAX_ERROR_SAMPLES = 5


@dataclass
class Prepared:
    rows: list[dict[str, Any]] = field(default_factory=list)
    skipped: int = 0
    duplicates: int = 0
    error_samples: list[str] = field(default_factory=list)


def _prepare(
    raws: Iterable[dict[str, Any]],
    normalize: Callable[[dict[str, Any]], dict[str, Any]],
    key: Callable[[dict[str, Any]], tuple[Any, ...]],
) -> Prepared:
    """정규화 실패는 건너뛰고, 같은 키는 마지막 값만 남긴다 (upsert 배치 내 중복 금지)."""
    result = Prepared()
    by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    for raw in raws:
        try:
            row = normalize(raw)
        except NormalizeError as exc:
            result.skipped += 1
            if len(result.error_samples) < MAX_ERROR_SAMPLES:
                result.error_samples.append(str(exc))
            continue
        k = key(row)
        if k in by_key:
            result.duplicates += 1
        by_key[k] = row
    result.rows = list(by_key.values())
    return result


def prepare_policies(raws: list[dict[str, Any]]) -> tuple[Prepared, Counter[tuple[str, str]]]:
    """반환: (정규화 결과, 코드표에 없는 (컬럼, 값) 빈도)."""
    prepared = _prepare(raws, normalize_policy, lambda r: (r["plcy_no"],))
    unknown: Counter[tuple[str, str]] = Counter()
    for raw in raws:
        unknown.update(find_unknown_codes(raw))
    return prepared, unknown


def prepare_companies(listing: FssListing) -> Prepared:
    return _prepare(
        listing.base_list,
        lambda raw: normalize_company(raw, listing.fin_grp_code),
        lambda r: (r["fin_co_no"],),
    )


@dataclass
class PreparedSaving:
    products: Prepared
    options: Prepared
    orphan_options: int = 0


def prepare_saving(listing: FssListing) -> PreparedSaving:
    """상품에 없는 옵션은 FK 위반이므로 버리고 orphan 으로 센다."""
    products = _prepare(
        listing.base_list,
        lambda raw: normalize_saving_product(raw, listing.fin_grp_code),
        lambda r: (r["fin_co_no"], r["fin_prdt_cd"]),
    )
    options = _prepare(
        listing.option_list,
        normalize_saving_option,
        lambda r: (
            r["fin_co_no"],
            r["fin_prdt_cd"],
            r["rate_type"],
            r["reserve_type"],
            r["term_months"],
        ),
    )
    product_keys = {(p["fin_co_no"], p["fin_prdt_cd"]) for p in products.rows}
    kept = [o for o in options.rows if (o["fin_co_no"], o["fin_prdt_cd"]) in product_keys]
    orphan = len(options.rows) - len(kept)
    options.rows = kept
    return PreparedSaving(products=products, options=options, orphan_options=orphan)


@dataclass
class SourceReport:
    source: str
    status: str = "running"
    fetched: int = 0
    upserted: int = 0
    pruned: int = 0
    skipped: int = 0
    duplicates: int = 0
    elapsed_sec: float = 0.0
    details: dict[str, int] = field(default_factory=dict)
    unknown_codes: Counter[tuple[str, str]] = field(default_factory=Counter)
    notes: list[str] = field(default_factory=list)
    error: str | None = None


async def _run(
    engine: AsyncEngine, report: SourceReport, body: Callable[[], Awaitable[None]]
) -> SourceReport:
    """ingest_runs 에 시작·종료를 기록한다. 실패해도 예외를 올리지 않아 다른 소스는 계속 돈다."""
    run_id = await loader.start_run(engine, report.source)
    started = time.perf_counter()
    try:
        await body()
    except Exception as exc:
        logger.exception("%s 적재 실패", report.source)
        report.status = "failed"
        report.error = f"{type(exc).__name__}: {exc}"
    report.elapsed_sec = time.perf_counter() - started
    await loader.finish_run(
        engine,
        run_id,
        status=report.status,
        fetched=report.fetched,
        upserted=report.upserted,
        error=report.error,
    )
    return report


async def run_ontong(engine: AsyncEngine, client: OntongClient) -> SourceReport:
    report = SourceReport(source="ontong")

    async def body() -> None:
        result = await client.fetch_all_policies()
        report.fetched = len(result.policies)
        report.details["api_total_count"] = result.total_count

        prepared, unknown = prepare_policies(result.policies)
        report.skipped, report.duplicates = prepared.skipped, prepared.duplicates
        report.unknown_codes = unknown
        report.notes.extend(prepared.error_samples)

        prune = result.is_complete
        if not prune:
            report.notes.append(
                f"전체 {result.total_count}건 중 {report.fetched}건만 수집되어"
                " 미수집 정책 삭제를 건너뜀"
            )
        async with engine.begin() as conn:
            report.upserted, report.pruned = await loader.load_policies(
                conn, prepared.rows, prune=prune
            )
        report.status = "success" if prune else "partial"

    return await _run(engine, report, body)


async def run_fss(
    engine: AsyncEngine,
    client: FssClient,
    fin_groups: Iterable[str] = DEFAULT_FIN_GROUPS,
) -> SourceReport:
    report = SourceReport(source="fss")

    async def body() -> None:
        fetched: list[tuple[str, Prepared, PreparedSaving, bool]] = []
        for grp in fin_groups:
            companies = await client.fetch_companies(grp)
            products = await client.fetch_saving_products(grp)
            report.fetched += len(companies.base_list) + len(products.base_list)
            complete = products.total_count > 0 and len(products.base_list) >= products.total_count
            if not complete:
                report.notes.append(f"권역 {grp} 상품 수집이 불완전해 미수집 상품 삭제를 건너뜀")
            fetched.append((grp, prepare_companies(companies), prepare_saving(products), complete))

        details: Counter[str] = Counter()
        async with engine.begin() as conn:
            for grp, companies, saving, complete in fetched:
                n_companies = await loader.load_companies(conn, companies.rows)
                n_products, n_options, n_pruned = await loader.load_saving_products(
                    conn, grp, saving.products.rows, saving.options.rows, prune=complete
                )
                report.upserted += n_companies + n_products
                report.pruned += n_pruned
                for prepared in (companies, saving.products, saving.options):
                    report.skipped += prepared.skipped
                    report.duplicates += prepared.duplicates
                    report.notes.extend(prepared.error_samples)
                details[f"{grp}_companies"] += n_companies
                details[f"{grp}_products"] += n_products
                details[f"{grp}_options"] += n_options
                details[f"{grp}_youth_products"] += sum(
                    1 for p in saving.products.rows if p["is_youth"]
                )
                details[f"{grp}_companies_with_homepage"] += sum(
                    1 for c in companies.rows if c["homepage_url"]
                )
                details[f"{grp}_orphan_options"] += saving.orphan_options
        report.details.update(details)
        report.status = "success" if all(c for *_, c in fetched) else "partial"

    return await _run(engine, report, body)


def format_report(report: SourceReport) -> str:
    lines = [
        f"[{report.source}] status={report.status} elapsed={report.elapsed_sec:.1f}s",
        f"  fetched={report.fetched} upserted={report.upserted} pruned={report.pruned}"
        f" skipped={report.skipped} duplicates={report.duplicates}",
    ]
    lines.extend(f"  {name}={value}" for name, value in sorted(report.details.items()))
    if report.unknown_codes:
        total = sum(report.unknown_codes.values())
        lines.append(f"  unknown_codes={total} (상위 20개)")
        lines.extend(
            f"    {column}={code!r}: {count}"
            for (column, code), count in report.unknown_codes.most_common(20)
        )
    lines.extend(f"  note: {note}" for note in report.notes)
    if report.error:
        lines.append(f"  error: {report.error}")
    return "\n".join(lines)
