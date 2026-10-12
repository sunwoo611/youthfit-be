from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.main import app


async def test_health_without_database_is_degraded(monkeypatch):
    # DATABASE_URL 없이 기동하는 상황을 고정 (로컬 .env 의 영향을 받지 않도록)
    monkeypatch.setattr(
        "app.api.health.get_settings",
        lambda: Settings(_env_file=None, database_url=None),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/health")

    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "degraded"
    assert body["database"] == "not_configured"
