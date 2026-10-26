from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncConnection

from app.api.deps import get_db_connection, get_today
from app.schemas.candidates import CandidateRequest, CandidateResponse
from app.services.candidate_service import find_candidates

router = APIRouter(prefix="/v1", tags=["candidates"])


@router.post("/candidates", response_model=CandidateResponse)
async def create_candidates(
    request: CandidateRequest,
    conn: Annotated[AsyncConnection, Depends(get_db_connection)],
    today: Annotated[date, Depends(get_today)],
) -> CandidateResponse:
    """정책(최대 50)·적금(최대 20) 후보를 고른다. 외부 API·LLM 은 호출하지 않는다."""
    return await find_candidates(conn, request, today)
