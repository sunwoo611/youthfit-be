import pytest

from app.core.config import Settings


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (
            "postgresql://u:p@host:5432/db",
            "postgresql+asyncpg://u:p@host:5432/db",
        ),
        (
            "postgres://u:p@host/db?sslmode=require",
            "postgresql+asyncpg://u:p@host/db?ssl=require",
        ),
        (
            "postgresql+asyncpg://u:p@127.0.0.1:5432/db",
            "postgresql+asyncpg://u:p@127.0.0.1:5432/db",
        ),
    ],
)
def test_database_url_is_converted_for_asyncpg(given: str, expected: str):
    assert Settings(_env_file=None, database_url=given).database_url == expected


def test_empty_database_url_stays_unconfigured():
    assert not Settings(_env_file=None, database_url="").database_url


def test_cors_origins_are_split_by_comma():
    settings = Settings(
        _env_file=None, cors_origins="http://localhost:3000, https://youthfit.vercel.app,"
    )
    assert settings.cors_origin_list == ["http://localhost:3000", "https://youthfit.vercel.app"]
