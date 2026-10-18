"""정규화된 행을 PostgreSQL 에 적재한다. 호출자가 트랜잭션을 연다."""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import Table, delete, func, insert, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.models import FinancialCompany, IngestRun, Policy, SavingProduct, SavingProductOption

# asyncpg 의 쿼리당 바인드 파라미터 상한(32767) 아래로 유지한다. 정책은 약 40컬럼.
BATCH_SIZE = 500
ERROR_MAX_LEN = 2000

_policies: Table = Policy.__table__  # type: ignore[assignment]
_companies: Table = FinancialCompany.__table__  # type: ignore[assignment]
_products: Table = SavingProduct.__table__  # type: ignore[assignment]
_options: Table = SavingProductOption.__table__  # type: ignore[assignment]
_runs: Table = IngestRun.__table__  # type: ignore[assignment]


def _chunks(rows: Sequence[dict[str, Any]], size: int) -> list[Sequence[dict[str, Any]]]:
    return [rows[i : i + size] for i in range(0, len(rows), size)]


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
) -> tuple[int, int]:
    """정책 upsert. prune 이면 이번 트랜잭션에서 갱신되지 않은 정책을 삭제한다.

    반환: (upsert 건수, 삭제 건수)
    """
    upserted = await upsert_rows(conn, _policies, rows, ["plcy_no"])
    pruned = 0
    if prune:
        # 같은 트랜잭션의 now() 는 고정이라, 이번에 upsert 된 행만 ingested_at == now() 이다.
        result = await conn.execute(delete(_policies).where(_policies.c.ingested_at < func.now()))
        pruned = result.rowcount or 0
    return upserted, pruned


async def load_companies(conn: AsyncConnection, rows: Sequence[dict[str, Any]]) -> int:
    return await upsert_rows(conn, _companies, rows, ["fin_co_no"])


async def load_saving_products(
    conn: AsyncConnection,
    fin_grp_code: str,
    products: Sequence[dict[str, Any]],
    options: Sequence[dict[str, Any]],
    *,
    prune: bool,
) -> tuple[int, int, int]:
    """권역 단위로 상품 upsert, 해당 권역 옵션은 전부 교체, prune 이면 사라진 상품 삭제.

    반환: (상품 upsert 건수, 옵션 insert 건수, 상품 삭제 건수)
    """
    upserted = await upsert_rows(conn, _products, products, ["fin_co_no", "fin_prdt_cd"])

    group_products = select(_products.c.fin_co_no, _products.c.fin_prdt_cd).where(
        _products.c.fin_grp_code == fin_grp_code
    )
    await conn.execute(
        delete(_options).where(
            tuple_(_options.c.fin_co_no, _options.c.fin_prdt_cd).in_(group_products)
        )
    )
    for chunk in _chunks(options, BATCH_SIZE):
        await conn.execute(insert(_options), list(chunk))

    pruned = 0
    if prune:
        result = await conn.execute(
            delete(_products).where(
                _products.c.fin_grp_code == fin_grp_code,
                _products.c.ingested_at < func.now(),
            )
        )
        pruned = result.rowcount or 0
    return upserted, len(options), pruned


async def start_run(engine: AsyncEngine, source: str) -> int:
    async with engine.begin() as conn:
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
