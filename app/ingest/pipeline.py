"""수집 → 정규화 → 적재 흐름과 소스별 실행 결과.

외부 API 호출은 DB 트랜잭션 밖에서 끝내고, 적재는 소스별 단일 트랜잭션으로 한다.
"""

import asyncio
import logging
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from app.ingest import loader
from app.ingest.normalize import NormalizeError, find_unknown_codes, normalize_policy
from app.ingest.ontong import OntongClient

logger = logging.getLogger(__name__)

MAX_ERROR_SAMPLES = 5


def is_complete_fetch(total_count: int, unique_fetched: int) -> bool:
    """API 가 알려준 전체 건수 이상을 중복 없이 받았는지. 미수집 행 삭제의 첫 번째 조건."""
    return total_count > 0 and unique_fetched >= total_count


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

    def apply_prune(self, label: str, complete: bool, result: loader.PruneResult | None) -> bool:
        """삭제 결과를 반영하고, 미수집 행 정리까지 끝났으면 True."""
        if not complete:
            self.notes.append(f"{label}: 수집이 불완전해 미수집 행 삭제를 건너뜀")
            return False
        if result is None:
            return False
        self.pruned += result.deleted
        if result.blocked:
            self.notes.append(
                f"{label}: 삭제 대상 {result.stale}건이 기존 {result.existing}건의"
                f" {loader.MAX_PRUNE_RATIO:.0%} 상한을 넘어 삭제를 건너뜀 (원본 확인 필요)"
            )
            return False
        return True


async def _run(
    engine: AsyncEngine, report: SourceReport, body: Callable[[], Awaitable[None]]
) -> SourceReport:
    """ingest_runs 에 시작·종료를 기록한다.

    일반 예외는 failed 로 기록하고 삼켜서, 호출자가 실행 결과를 출력한 뒤 종료 코드로 알리게 한다.
    Ctrl+C·작업 취소는 cancelled 로 기록한 뒤 다시 올려 프로그램을 멈춘다.
    """
    run_id = await loader.start_run(engine, report.source)
    started = time.perf_counter()
    interrupted: BaseException | None = None
    try:
        await body()
    except (asyncio.CancelledError, KeyboardInterrupt) as exc:
        report.status = "cancelled"
        report.error = "사용자 중단 또는 작업 취소"
        interrupted = exc
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
    if interrupted is not None:
        raise interrupted
    return report


async def run_ontong(engine: AsyncEngine, client: OntongClient) -> SourceReport:
    report = SourceReport(source="ontong")

    async def body() -> None:
        result = await client.fetch_all_policies()
        report.fetched = len(result.policies)
        report.details["api_total_count"] = result.total_count

        unique = len({p.get("plcyNo") for p in result.policies})
        report.details["unique_fetched"] = unique

        prepared, unknown = prepare_policies(result.policies)
        report.skipped, report.duplicates = prepared.skipped, prepared.duplicates
        report.unknown_codes = unknown
        report.notes.extend(prepared.error_samples)

        complete = is_complete_fetch(result.total_count, unique)
        async with engine.begin() as conn:
            report.upserted, pruned = await loader.load_policies(
                conn, prepared.rows, prune=complete
            )
        label = f"정책(전체 {result.total_count}건 중 {unique}건 수집)"
        report.status = "success" if report.apply_prune(label, complete, pruned) else "partial"

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
