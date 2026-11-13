from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.models import Base

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"

EXPECTED_TABLES = {
    "policies",
    "ingest_runs",
    "sessions",
}


def _script_directory() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))


def test_metadata_has_expected_tables():
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_policy_array_columns_have_gin_indexes():
    policies = Base.metadata.tables["policies"]
    gin = {
        idx.name for idx in policies.indexes if idx.dialect_options["postgresql"]["using"] == "gin"
    }
    assert gin == {"ix_policies_categories", "ix_policies_sido_codes", "ix_policies_job_codes"}


def test_alembic_has_single_head():
    assert len(_script_directory().get_heads()) == 1


async def test_migration_matches_models():
    """DB 가 이미 head 로 마이그레이션된 경우에만 모델과 실제 스키마 차이를 비교한다 (읽기 전용)."""
    url = get_settings().database_url
    if not url:
        pytest.skip("DATABASE_URL 미설정")

    engine = create_async_engine(url, connect_args={"timeout": 2})
    try:
        async with engine.connect() as conn:

            def _diff(sync_conn):
                ctx = MigrationContext.configure(sync_conn, opts={"compare_type": True})
                return ctx.get_current_revision(), compare_metadata(ctx, Base.metadata)

            current, diff = await conn.run_sync(_diff)
    except (OSError, TimeoutError) as exc:
        pytest.skip(f"DB 연결 불가: {exc}")
    finally:
        await engine.dispose()

    if current != _script_directory().get_current_head():
        pytest.skip("DB 가 head 로 마이그레이션되지 않음 (uv run alembic upgrade head)")
    assert diff == []
