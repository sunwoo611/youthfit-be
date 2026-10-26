from collections.abc import AsyncIterator
from datetime import date, datetime

from fastapi import HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.config import get_settings
from app.core.database import get_engine
from app.ingest.normalize import KST


async def get_db_connection() -> AsyncIterator[AsyncConnection]:
    engine = get_engine(get_settings())
    if engine is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "DATABASE_URL 미설정")
    try:
        conn = await engine.connect()
    except (OSError, SQLAlchemyError) as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "DB 연결 불가") from exc
    try:
        yield conn
    finally:
        await conn.close()


def get_today() -> date:
    """마감 판정 기준일 (한국 날짜)."""
    return datetime.now(KST).date()
