from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.database import check_db

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    env: str
    database: Literal["up", "down", "not_configured"]


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    settings = get_settings()
    if not settings.database_url:
        return HealthResponse(status="degraded", env=settings.app_env, database="not_configured")

    db_up = await check_db(settings)
    return HealthResponse(
        status="ok" if db_up else "degraded",
        env=settings.app_env,
        database="up" if db_up else "down",
    )
