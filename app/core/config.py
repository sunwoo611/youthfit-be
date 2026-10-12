from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "YouthFit API"
    app_env: str = "local"

    # 미설정(None)이면 DB 없이 서버만 기동하고 /health 는 degraded 로 응답한다.
    database_url: str | None = None
    db_connect_timeout_sec: float = 2.0

    # 2차 Build 이후 사용 (공공 API 수집 / LLM)
    ontong_youth_api_key: str | None = None
    fss_api_key: str | None = None
    openai_api_key: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
