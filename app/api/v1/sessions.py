import logging
import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.api.deps import (
    get_db_connection,
    get_db_connector,
    get_db_transaction,
    get_llm_client,
    get_today,
)
from app.api.rate_limit import RateLimit
from app.core.database import ConnectionFactory
from app.llm.client import LlmClient
from app.schemas.sessions import (
    AnswerRequest,
    RecommendationResponse,
    SessionCreateRequest,
    SessionResponse,
)
from app.services import recommendation_service, session_service
from app.services.session_service import InvalidAnswerError, SessionNotFoundError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])

ReadConn = Annotated[AsyncConnection, Depends(get_db_connection)]
WriteConn = Annotated[AsyncConnection, Depends(get_db_transaction, scope="function")]

# 쓰기 경로만 제한한다. 생성은 전체 후보 조회 + INSERT 라 가장 무겁다.
CREATE_LIMIT = RateLimit(per_ip=20, total=300, window_sec=600)
ANSWER_LIMIT = RateLimit(per_ip=60, total=900, window_sec=600)
# 캐시가 없거나 LLM 이 실패하면 요청마다 LLM 을 부르므로 비용 상한을 겸해 가장 좁게 둔다.
REASON_LIMIT = RateLimit(per_ip=10, total=120, window_sec=600)

_NOT_FOUND = "세션이 없거나 만료됨"


@router.post(
    "",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(CREATE_LIMIT)],
)
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


@router.post(
    "/{session_id}/answers",
    response_model=SessionResponse,
    dependencies=[Depends(ANSWER_LIMIT)],
)
async def answer_session(
    session_id: uuid.UUID, request: AnswerRequest, conn: WriteConn
) -> SessionResponse:
    """답을 1차 조건에 반영해 후보를 다시 고른다. 다시 내면 이전 답을 덮어쓴다."""
    try:
        return await session_service.answer_session(conn, session_id, request)
    except SessionNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND) from exc
    except InvalidAnswerError as exc:
        # 422 상수 이름이 Starlette 0.48 에서 바뀌어 버전에 따라 없거나 경고가 나므로 숫자를 쓴다.
        raise HTTPException(422, str(exc)) from exc


@router.post(
    "/{session_id}/recommendations",
    response_model=RecommendationResponse,
    dependencies=[Depends(REASON_LIMIT)],
)
async def recommend(
    session_id: uuid.UUID,
    connect: Annotated[ConnectionFactory, Depends(get_db_connector)],
    llm: Annotated[LlmClient | None, Depends(get_llm_client)],
) -> RecommendationResponse:
    """답을 반영한 최종 후보 상위 정책과 선정 사유. 링크·마감일·자격은 DB 값이다.

    같은 답이면 저장해 둔 사유를 다시 쓴다. LLM 이 없거나 실패하면 템플릿 사유
    (`reason_source: "template"`)로 응답하고 저장하지 않는다.
    """
    try:
        return await recommendation_service.recommend(connect, llm, session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND) from exc
    except (OSError, SQLAlchemyError) as exc:
        # 사유 저장 실패는 서비스가 삼키므로 여기 오는 것은 세션·후보 읽기 실패다.
        # SQL 오류도 섞이는데 HTTPException 은 traceback 을 남기지 않으므로 기록한다.
        logger.exception("세션 %s 선정 사유 조회 실패", session_id)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "DB 연결 불가") from exc
