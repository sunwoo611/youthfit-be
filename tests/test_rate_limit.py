from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.rate_limit import RateLimit


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _limit(clock: _Clock, *, per_ip: int = 2, total: int = 5) -> RateLimit:
    return RateLimit(per_ip=per_ip, total=total, window_sec=60, clock=clock)


def test_blocks_ip_over_limit_until_window_passes():
    clock = _Clock()
    limit = _limit(clock)
    assert limit.hit("1.1.1.1") is None
    clock.now += 10
    assert limit.hit("1.1.1.1") is None
    clock.now += 10
    assert limit.hit("1.1.1.1") == 40
    assert limit.hit("2.2.2.2") is None

    clock.now += 40
    assert limit.hit("1.1.1.1") is None


def test_total_cap_applies_across_ips():
    clock = _Clock()
    limit = _limit(clock, per_ip=100, total=3)
    assert [limit.hit(f"10.0.0.{i}") for i in range(3)] == [None] * 3
    assert limit.hit("10.0.0.99") == 60


def test_blocked_requests_are_not_recorded():
    clock = _Clock()
    limit = _limit(clock, per_ip=2, total=5)
    for _ in range(10):
        limit.hit("1.1.1.1")
    # 한 IP가 계속 보내도 전체 예산은 그 IP 상한(2)만큼만 쓴다.
    assert [limit.hit(f"10.0.0.{i}") for i in range(3)] == [None] * 3
    assert limit.hit("10.0.0.9") is not None


def test_expired_ips_are_forgotten():
    clock = _Clock()
    limit = _limit(clock)
    limit.hit("1.1.1.1")
    clock.now += 61
    limit.hit("2.2.2.2")
    assert set(limit._by_ip) == {"2.2.2.2"}
    assert len(limit._all) == 1


async def test_dependency_returns_429_with_retry_after():
    limit = _limit(_Clock(), per_ip=1)
    mini = FastAPI()

    @mini.post("/x", dependencies=[Depends(limit)])
    async def endpoint() -> None:
        return None

    transport = ASGITransport(app=mini, client=("1.1.1.1", 123))
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        assert (await c.post("/x")).status_code == 200
        res = await c.post("/x")
    assert res.status_code == 429
    assert res.headers["Retry-After"] == "60"
