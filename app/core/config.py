from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "YouthFit API"
    app_env: str = "local"

    # 미설정(None)이면 DB 없이 서버만 기동하고 /health 는 degraded 로 응답한다.
    database_url: str | None = None
    db_connect_timeout_sec: float = 2.0

    # 쉼표로 구분한 브라우저 허용 출처. 주소가 바뀌는 Vercel 프리뷰는 정규식으로 허용한다.
    cors_origins: str = "http://localhost:3000"
    cors_origin_regex: str | None = None

    # 2차 Build 이후 사용 (공공 API 수집 / LLM)
    ontong_youth_api_key: str | None = None
    fss_api_key: str | None = None
    openai_api_key: str | None = None

    @field_validator("database_url")
    @classmethod
    def _use_asyncpg_url(cls, url: str | None) -> str | None:
        """Render 등이 주는 libpq 형식(`postgres://`, `?sslmode=`)을 asyncpg 형식으로 바꾼다."""
        if not url:
            return url
        parts = urlsplit(url)
        scheme = parts.scheme
        if scheme in ("postgres", "postgresql"):
            scheme = "postgresql+asyncpg"
        query = urlencode(
            [("ssl" if key == "sslmode" else key, value) for key, value in parse_qsl(parts.query)]
        )
        return urlunsplit(parts._replace(scheme=scheme, query=query))

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
