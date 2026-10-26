from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.main import create_app


def _preflight_headers(origin: str) -> dict[str, str]:
    return {
        "Origin": origin,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
    }


async def _preflight(settings: Settings, origin: str):
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.options("/v1/candidates", headers=_preflight_headers(origin))


async def test_preflight_allows_listed_origin():
    settings = Settings(_env_file=None, cors_origins="http://localhost:3000")
    res = await _preflight(settings, "http://localhost:3000")

    assert res.status_code == 200
    assert res.headers["access-control-allow-origin"] == "http://localhost:3000"


async def test_preflight_allows_origin_matching_regex():
    settings = Settings(
        _env_file=None,
        cors_origins="",
        cors_origin_regex=r"https://youthfit-fe-[a-z0-9-]+\.vercel\.app",
    )
    res = await _preflight(settings, "https://youthfit-fe-git-main-team.vercel.app")

    assert res.headers["access-control-allow-origin"] == (
        "https://youthfit-fe-git-main-team.vercel.app"
    )


async def test_preflight_rejects_unknown_origin():
    settings = Settings(_env_file=None, cors_origins="http://localhost:3000")
    res = await _preflight(settings, "https://evil.example.com")

    assert res.status_code == 400
    assert "access-control-allow-origin" not in res.headers
