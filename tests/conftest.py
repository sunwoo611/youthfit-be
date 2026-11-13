from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.core.config import get_settings
from app.models import Policy, QuestionSession


@pytest.fixture(autouse=True)
def _no_real_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """`.env` 에 OPENAI_API_KEY 가 있어도 테스트가 실제 API 를 부르지 않게 한다.

    가짜 클라이언트가 필요한 테스트는 `dependency_overrides[get_llm_client]` 로 바꿔 끼운다.
    """
    monkeypatch.setattr("app.api.deps.get_openai_client", lambda _settings: None)


@pytest.fixture
async def db_conn() -> AsyncIterator[AsyncConnection]:
    """정책·세션 테이블을 비운 트랜잭션. 끝나면 롤백해 적재 데이터를 건드리지 않는다."""
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
        for name in ("policies", "sessions"):
            if await connection.scalar(text(f"SELECT to_regclass('public.{name}')")) is None:
                pytest.skip("스키마 미적용 (uv run alembic upgrade head)")
        for table in (Policy.__table__, QuestionSession.__table__):
            await connection.execute(delete(table))
        yield connection
    finally:
        await trans.rollback()
        await connection.close()
        await engine.dispose()
