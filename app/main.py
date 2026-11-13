from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import health
from app.api.v1 import candidates, sessions
from app.core.config import Settings, get_settings
from app.core.database import dispose_engine
from app.llm.client import close_openai_client


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await dispose_engine()
    await close_openai_client()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_origin_regex=settings.cors_origin_regex or None,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health.router)
    app.include_router(candidates.router)
    app.include_router(sessions.router)
    return app


app = create_app()
