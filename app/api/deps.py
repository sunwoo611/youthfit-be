from collections.abc import AsyncIterator
from datetime import date, datetime
from typing import Annotated

from fastapi import Depends, HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.config import get_settings
from app.core.database import get_engine
from app.ingest.normalize import KST
from app.llm.client import LlmClient, get_openai_client


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


async def get_db_transaction(
    conn: Annotated[AsyncConnection, Depends(get_db_connection)],
) -> AsyncIterator[AsyncConnection]:
    """쓰기용 연결. 경로 함수가 예외 없이 끝나면 커밋하고, 예외면 롤백한다.

    커밋 실패가 응답에 드러나도록 `Depends(get_db_transaction, scope="function")` 로 쓴다.
    """
    async with conn.begin():
        yield conn


def get_llm_client() -> LlmClient | None:
    """OPENAI_API_KEY 미설정이면 None. 서비스는 None 이면 템플릿 경로를 쓴다."""
    return get_openai_client(get_settings())


def get_today() -> date:
    """마감 판정 기준일 (한국 날짜)."""
    return datetime.now(KST).date()
