import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncConnection

from app.api.deps import get_db_connection, get_db_transaction, get_today
from app.schemas.sessions import AnswerRequest, SessionCreateRequest, SessionResponse
from app.services import session_service
from app.services.session_service import InvalidAnswerError, SessionNotFoundError

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])

ReadConn = Annotated[AsyncConnection, Depends(get_db_connection)]
WriteConn = Annotated[AsyncConnection, Depends(get_db_transaction, scope="function")]

_NOT_FOUND = "세션이 없거나 만료됨"


@router.post("", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
async def create_session(
    request: SessionCreateRequest,
    conn: WriteConn,
    today: Annotated[date, Depends(get_today)],
) -> SessionResponse:
    """1차 후보와 후보를 가르는 역질문(최대 5개)을 만든다. 세션은 24시간 유지된다."""
    return await session_service.create_session(conn, request, today)


@router.get("/{session_id}", response_model=SessionResponse)
async def get_session(session_id: uuid.UUID, conn: ReadConn) -> SessionResponse:
    """저장된 질문·답과 그 답을 반영한 후보."""
    try:
        return await session_service.get_session(conn, session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND) from exc


@router.post("/{session_id}/answers", response_model=SessionResponse)
async def answer_session(
    session_id: uuid.UUID, request: AnswerRequest, conn: WriteConn
) -> SessionResponse:
    """답을 1차 조건에 반영해 후보를 다시 고른다. 다시 내면 이전 답을 덮어쓴다."""
    try:
        return await session_service.answer_session(conn, session_id, request)
    except SessionNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND) from exc
    except InvalidAnswerError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
