from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import Settings

_engine: AsyncEngine | None = None


def get_engine(settings: Settings) -> AsyncEngine | None:
    """DATABASE_URL 이 설정된 경우에만 엔진을 지연 생성한다."""
    global _engine
    if not settings.database_url:
        return None
    if _engine is None:
        _engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            connect_args={"timeout": settings.db_connect_timeout_sec},
        )
    return _engine


async def dispose_engine() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


async def check_db(settings: Settings) -> bool:
    """DB 연결 가능 여부. 미설정이거나 실패하면 False."""
    engine = get_engine(settings)
    if engine is None:
        return False
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
