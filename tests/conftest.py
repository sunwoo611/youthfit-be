from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.core.config import get_settings
from app.models import FinancialCompany, Policy, SavingProduct, SavingProductOption


@pytest.fixture
async def db_conn() -> AsyncIterator[AsyncConnection]:
    """정책·적금 테이블을 비운 트랜잭션. 테스트가 끝나면 롤백해 적재 데이터를 건드리지 않는다."""
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
        for table in (
            SavingProductOption.__table__,
            SavingProduct.__table__,
            FinancialCompany.__table__,
            Policy.__table__,
        ):
            await connection.execute(delete(table))
        yield connection
    finally:
        await trans.rollback()
        await connection.close()
        await engine.dispose()
