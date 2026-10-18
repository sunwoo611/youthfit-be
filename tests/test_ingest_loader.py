"""적재 계층 통합 테스트. 마이그레이션된 DB 가 있을 때만 돌고, 매 테스트를 롤백한다.

테스트마다 트랜잭션 안에서 적재 테이블을 비우고 시작해 기존 데이터와 무관하게 동작한다.
"""

import json
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.core.config import get_settings
from app.ingest import loader
from app.ingest.normalize import (
    normalize_policy,
    normalize_saving_option,
    normalize_saving_product,
)
from app.models import FinancialCompany, IngestRun, Policy, SavingProduct, SavingProductOption

SAMPLES = Path(__file__).resolve().parents[1] / "docs" / "data-samples"

policies = Policy.__table__
products = SavingProduct.__table__
options = SavingProductOption.__table__
runs = IngestRun.__table__
YESTERDAY = datetime.now(UTC) - timedelta(days=1)


@pytest.fixture
async def conn() -> AsyncIterator[AsyncConnection]:
    url = get_settings().database_url
    if not url:
        pytest.skip("DATABASE_URL 미설정")
    engine = create_async_engine(url, connect_args={"timeout": 2})
    try:
        connection = await engine.connect()
    except (OSError, TimeoutError) as exc:
        await engine.dispose()
        pytest.skip(f"DB 연결 불가: {type(exc).__name__}")
    trans = await connection.begin()
    try:
        if await connection.scalar(text("SELECT to_regclass('public.policies')")) is None:
            pytest.skip("스키마 미적용 (uv run alembic upgrade head)")
        for table in (options, products, FinancialCompany.__table__, policies, runs):
            await connection.execute(delete(table))
        yield connection
    finally:
        await trans.rollback()
        await connection.close()
        await engine.dispose()


def _policy(plcy_no: str, **overrides: str) -> dict:
    body = json.loads((SAMPLES / "ontong-getPlcy-sample.json").read_text(encoding="utf-8"))
    raw = {**body["result"]["youthPolicyList"][1], "plcyNo": plcy_no, **overrides}
    return normalize_policy(raw)


def _saving(fin_prdt_cd: str, grp: str = "020000") -> tuple[dict, list[dict]]:
    body = json.loads(
        (SAMPLES / "fss-savingProductsSearch-sample.json").read_text(encoding="utf-8")
    )["result"]
    base = {**body["baseList"][0], "fin_co_no": "TEST001", "fin_prdt_cd": fin_prdt_cd}
    opts = [
        {**o, "fin_co_no": "TEST001", "fin_prdt_cd": fin_prdt_cd}
        for o in body["optionList"]
        if o["fin_prdt_cd"] == "WR0001F"
    ]
    return normalize_saving_product(base, grp), [normalize_saving_option(o) for o in opts]


async def _seed_old_policies(conn: AsyncConnection, n: int) -> list[str]:
    ids = [f"TEST-{i:02d}" for i in range(n)]
    await conn.execute(insert(policies), [{**_policy(i), "ingested_at": YESTERDAY} for i in ids])
    return ids


async def _policy_ids(conn: AsyncConnection) -> list[str]:
    return sorted((await conn.execute(select(policies.c.plcy_no))).scalars().all())


async def _option_counts(conn: AsyncConnection) -> dict[str, int]:
    stmt = select(options.c.fin_prdt_cd, func.count()).group_by(options.c.fin_prdt_cd)
    return dict((await conn.execute(stmt)).all())


async def test_upsert_policies_inserts_then_updates(conn: AsyncConnection):
    await loader.load_policies(conn, [_policy("TEST-P1")], prune=False)
    upserted, pruned = await loader.load_policies(
        conn, [_policy("TEST-P1", plcyNm="바뀐 이름")], prune=False
    )

    row = (await conn.execute(select(policies).where(policies.c.plcy_no == "TEST-P1"))).one()
    assert (upserted, pruned) == (1, None)
    assert row.name == "바뀐 이름"
    assert row.sido_codes == ["44"]
    assert row.job_codes == ["0013003"]
    assert row.apply_end == date(2026, 6, 30)
    assert row.raw["plcyNo"] == "TEST-P1"


async def test_first_load_into_empty_table_prunes_nothing(conn: AsyncConnection):
    _, pruned = await loader.load_policies(conn, [_policy("TEST-P1")], prune=True)
    assert pruned == loader.PruneResult(existing=0, stale=0, deleted=0)
    assert not pruned.blocked


async def test_prune_within_ratio_deletes_stale(conn: AsyncConnection):
    ids = await _seed_old_policies(conn, 20)

    _, pruned = await loader.load_policies(conn, [_policy(i) for i in ids[:19]], prune=True)

    assert pruned == loader.PruneResult(existing=20, stale=1, deleted=1)
    assert await _policy_ids(conn) == ids[:19]


async def test_prune_over_ratio_is_blocked_and_keeps_everything(conn: AsyncConnection):
    ids = await _seed_old_policies(conn, 20)

    _, pruned = await loader.load_policies(conn, [_policy(i) for i in ids[:18]], prune=True)

    assert pruned == loader.PruneResult(existing=20, stale=2, deleted=0)
    assert pruned.blocked
    assert await _policy_ids(conn) == ids


async def test_tiny_api_response_cannot_wipe_mirror(conn: AsyncConnection):
    """API 가 일시적으로 1건만 돌려준 상황. 기존 미러는 그대로여야 한다."""
    ids = await _seed_old_policies(conn, 30)

    _, pruned = await loader.load_policies(conn, [_policy("TEST-NEW")], prune=True)

    assert pruned.blocked
    assert await _policy_ids(conn) == sorted([*ids, "TEST-NEW"])


async def test_prune_false_never_deletes(conn: AsyncConnection):
    ids = await _seed_old_policies(conn, 3)

    _, pruned = await loader.load_policies(conn, [], prune=False)

    assert pruned is None
    assert await _policy_ids(conn) == ids


async def test_partial_saving_load_keeps_options_of_unfetched_products(conn: AsyncConnection):
    a, a_opts = _saving("TEST-A")
    b, b_opts = _saving("TEST-B")
    await loader.load_saving_products(conn, "020000", [a, b], a_opts + b_opts, prune=False)

    upserted, n_options, pruned = await loader.load_saving_products(
        conn, "020000", [a], a_opts[:2], prune=False
    )

    assert (upserted, n_options, pruned) == (1, 2, None)
    assert await _option_counts(conn) == {"TEST-A": 2, "TEST-B": len(b_opts)}


async def test_saving_prune_is_scoped_to_group_and_cascades_options(conn: AsyncConnection):
    group = [_saving(f"TEST-{i:02d}") for i in range(20)]
    other, other_opts = _saving("TEST-OTHER", grp="030300")
    await loader.load_saving_products(conn, "030300", [other], other_opts, prune=False)
    await loader.load_saving_products(
        conn, "020000", [p for p, _ in group], [o for _, os in group for o in os], prune=False
    )
    await conn.execute(products.update().values(ingested_at=YESTERDAY))

    kept = group[:19]
    _, _, pruned = await loader.load_saving_products(
        conn, "020000", [p for p, _ in kept], [o for _, os in kept for o in os], prune=True
    )

    codes = dict(
        (await conn.execute(select(products.c.fin_prdt_cd, products.c.fin_grp_code))).all()
    )
    counts = await _option_counts(conn)
    assert pruned == loader.PruneResult(existing=20, stale=1, deleted=1)
    assert "TEST-19" not in codes and "TEST-19" not in counts
    assert codes["TEST-OTHER"] == "030300"
    assert counts["TEST-OTHER"] == len(other_opts)


async def test_abandon_stale_runs_only_touches_old_running_rows_of_same_source(
    conn: AsyncConnection,
):
    now = datetime.now(UTC)
    old = now - loader.STALE_RUN_AFTER - timedelta(minutes=1)
    await conn.execute(
        insert(runs),
        [
            {"source": "ontong", "status": "running", "started_at": old},
            {"source": "ontong", "status": "running", "started_at": now},
            {"source": "ontong", "status": "success", "started_at": old},
            {"source": "fss", "status": "running", "started_at": old},
        ],
    )

    assert await loader.abandon_stale_runs(conn, "ontong") == 1

    rows = (await conn.execute(select(runs.c.source, runs.c.status).order_by(runs.c.id))).all()
    assert [tuple(r) for r in rows] == [
        ("ontong", "abandoned"),
        ("ontong", "running"),
        ("ontong", "success"),
        ("fss", "running"),
    ]
