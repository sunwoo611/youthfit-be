"""적재 계층 통합 테스트. 마이그레이션된 DB 가 있을 때만 돌고, 매 테스트를 롤백한다."""

import json
from collections.abc import AsyncIterator
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.core.config import get_settings
from app.ingest import loader
from app.ingest.normalize import (
    normalize_policy,
    normalize_saving_option,
    normalize_saving_product,
)
from app.models import Policy, SavingProduct, SavingProductOption

SAMPLES = Path(__file__).resolve().parents[1] / "docs" / "data-samples"

policies = Policy.__table__
products = SavingProduct.__table__
options = SavingProductOption.__table__


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


async def test_upsert_policies_inserts_then_updates(conn: AsyncConnection):
    await loader.load_policies(conn, [_policy("TEST-P1")], prune=False)
    upserted, pruned = await loader.load_policies(
        conn, [_policy("TEST-P1", plcyNm="바뀐 이름")], prune=False
    )

    row = (await conn.execute(select(policies).where(policies.c.plcy_no == "TEST-P1"))).one()
    assert (upserted, pruned) == (1, 0)
    assert row.name == "바뀐 이름"
    assert row.sido_codes == ["44"]
    assert row.job_codes == ["0013003"]
    assert row.apply_end == date(2026, 6, 30)
    assert row.raw["plcyNo"] == "TEST-P1"


async def test_load_policies_prune_removes_rows_not_in_this_run(conn: AsyncConnection):
    stale = {**_policy("TEST-STALE"), "ingested_at": func.now() - timedelta(days=1)}
    await conn.execute(insert(policies).values(stale))

    _, pruned_without = await loader.load_policies(conn, [_policy("TEST-P1")], prune=False)
    assert pruned_without == 0
    assert await conn.scalar(select(func.count()).where(policies.c.plcy_no == "TEST-STALE")) == 1

    _, pruned = await loader.load_policies(conn, [_policy("TEST-P1")], prune=True)
    remaining = (await conn.execute(select(policies.c.plcy_no))).scalars().all()
    assert pruned >= 1
    assert remaining == ["TEST-P1"]


async def test_load_saving_products_replaces_options_and_prunes_within_group(
    conn: AsyncConnection,
):
    p1, p1_opts = _saving("TEST-A")
    p2, p2_opts = _saving("TEST-B")
    other, other_opts = _saving("TEST-OTHER", grp="030300")
    await loader.load_saving_products(conn, "030300", [other], other_opts, prune=False)
    await loader.load_saving_products(conn, "020000", [p1, p2], p1_opts + p2_opts, prune=False)
    await conn.execute(
        products.update()
        .where(products.c.fin_prdt_cd == "TEST-B")
        .values(ingested_at=func.now() - timedelta(days=1))
    )

    upserted, n_options, pruned = await loader.load_saving_products(
        conn, "020000", [p1], p1_opts[:2], prune=True
    )

    codes_by_group = dict(
        (await conn.execute(select(products.c.fin_prdt_cd, products.c.fin_grp_code))).all()
    )
    option_counts = dict(
        (
            await conn.execute(
                select(options.c.fin_prdt_cd, func.count()).group_by(options.c.fin_prdt_cd)
            )
        ).all()
    )
    assert (upserted, n_options) == (1, 2)
    assert pruned >= 1
    assert "TEST-B" not in codes_by_group
    assert codes_by_group["TEST-OTHER"] == "030300"
    assert option_counts["TEST-A"] == 2
    assert option_counts["TEST-OTHER"] == len(other_opts)
    assert "TEST-B" not in option_counts
