"""정규화된 행을 PostgreSQL 에 적재한다.

`start_run`·`finish_run` 외에는 호출자가 트랜잭션을 연다.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, TypeVar

from sqlalchemy import ColumnElement, Table, delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.models import IngestRun, Policy

# asyncpg 의 쿼리당 바인드 파라미터 상한(32767) 아래로 유지한다. 정책은 약 40컬럼.
BATCH_SIZE = 500
ERROR_MAX_LEN = 2000

# 한 번의 적재에서 지울 수 있는 행은 기존 행의 5% 까지. API 가 일시적으로 적은 건수를 돌려줘도
# 미러가 비지 않게 하는 안전장치다. 넘으면 아무것도 지우지 않는다.
MAX_PRUNE_RATIO = 0.05

# 이 시간보다 오래 running 으로 남은 실행 기록은 강제 종료된 것으로 본다 (일 1회 배치 기준).
STALE_RUN_AFTER = timedelta(hours=6)

_policies: Table = Policy.__table__  # type: ignore[assignment]
_runs: Table = IngestRun.__table__  # type: ignore[assignment]

T = TypeVar("T")


@dataclass(frozen=True)
class PruneResult:
    existing: int
    stale: int
    deleted: int

    @property
    def blocked(self) -> bool:
        return self.stale > 0 and self.deleted == 0


def prune_allowed(stale: int, existing: int, max_ratio: float = MAX_PRUNE_RATIO) -> bool:
    """삭제 대상(stale)이 적재 전 기존 행(existing)의 max_ratio 이하일 때만 허용."""
    if stale == 0:
        return True
    return existing > 0 and stale <= existing * max_ratio


def _chunks(items: Sequence[T], size: int) -> list[Sequence[T]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


async def _count(conn: AsyncConnection, table: Table, *where: ColumnElement[bool]) -> int:
    stmt = select(func.count()).select_from(table)
    if where:
        stmt = stmt.where(*where)
    return (await conn.scalar(stmt)) or 0


async def _prune_stale(
    conn: AsyncConnection, table: Table, scope: Sequence[ColumnElement[bool]], existing: int
) -> PruneResult:
    """이번 트랜잭션에서 갱신되지 않은 행을 상한 안에서만 삭제한다.

    같은 트랜잭션의 now() 는 고정이라, 이번에 upsert 된 행만 ingested_at == now() 이다.
    """
    stale_where = [*scope, table.c.ingested_at < func.now()]
    stale = await _count(conn, table, *stale_where)
    if stale == 0 or not prune_allowed(stale, existing):
        return PruneResult(existing=existing, stale=stale, deleted=0)
    result = await conn.execute(delete(table).where(*stale_where))
    return PruneResult(existing=existing, stale=stale, deleted=result.rowcount or 0)


async def upsert_rows(
    conn: AsyncConnection,
    table: Table,
    rows: Sequence[dict[str, Any]],
    key_columns: Sequence[str],
) -> int:
    """`INSERT ... ON CONFLICT DO UPDATE`. 같은 배치 안에 중복 키가 있으면 안 된다."""
    if not rows:
        return 0
    update_columns = [
        c.name for c in table.columns if c.name not in key_columns and c.name in rows[0]
    ]
    for chunk in _chunks(rows, BATCH_SIZE):
        stmt = pg_insert(table).values(list(chunk))
        set_: dict[str, Any] = {name: stmt.excluded[name] for name in update_columns}
        if "ingested_at" in table.c:
            set_["ingested_at"] = func.now()
        await conn.execute(stmt.on_conflict_do_update(index_elements=key_columns, set_=set_))
    return len(rows)


async def load_policies(
    conn: AsyncConnection, rows: Sequence[dict[str, Any]], *, prune: bool
) -> tuple[int, PruneResult | None]:
    """정책 upsert. prune 이면 이번에 갱신되지 않은 정책을 상한 안에서 삭제한다."""
    existing = await _count(conn, _policies) if prune else 0
    upserted = await upsert_rows(conn, _policies, rows, ["plcy_no"])
    pruned = await _prune_stale(conn, _policies, [], existing) if prune else None
    return upserted, pruned


async def abandon_stale_runs(conn: AsyncConnection, source: str) -> int:
    """STALE_RUN_AFTER 보다 오래 running 인 같은 소스의 기록을 abandoned 로 바꾼다."""
    result = await conn.execute(
        update(_runs)
        .where(
            _runs.c.source == source,
            _runs.c.status == "running",
            _runs.c.started_at < func.now() - STALE_RUN_AFTER,
        )
        .values(
            status="abandoned",
            finished_at=func.now(),
            error="종료 기록 없이 끝난 실행 (강제 종료 추정)",
        )
    )
    return result.rowcount or 0


async def start_run(engine: AsyncEngine, source: str) -> int:
    """실행 기록을 만든다. 같은 소스에서 오래 running 으로 남은 기록은 먼저 정리한다."""
    async with engine.begin() as conn:
        await abandon_stale_runs(conn, source)
        result = await conn.execute(
            insert(_runs).values(source=source, status="running").returning(_runs.c.id)
        )
        return result.scalar_one()


async def finish_run(
    engine: AsyncEngine,
    run_id: int,
    *,
    status: str,
    fetched: int = 0,
    upserted: int = 0,
    error: str | None = None,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            update(_runs)
            .where(_runs.c.id == run_id)
            .values(
                status=status,
                finished_at=func.now(),
                fetched_count=fetched,
                upserted_count=upserted,
                error=error[:ERROR_MAX_LEN] if error else None,
            )
        )
