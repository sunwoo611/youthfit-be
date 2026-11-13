"""요청 제한. 인스턴스가 하나(Render 무료)라 메모리에 센다.

인스턴스를 늘리면 인스턴스마다 따로 세므로 공유 저장소(Redis 등)로 옮겨야 한다.
서버가 `--forwarded-allow-ips='*'` 로 떠서 클라이언트가 IP를 속일 수 있으므로,
IP별 제한과 함께 전체 상한을 둔다. 막힌 요청은 기록하지 않아 IP를 바꿔 가며 보내도
기록되는 IP 수가 전체 상한을 넘지 않는다.
"""

import math
import time
from collections import deque
from collections.abc import Callable

from fastapi import HTTPException, Request, status


class RateLimit:
    def __init__(
        self,
        *,
        per_ip: int,
        total: int,
        window_sec: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.per_ip = per_ip
        self.total = total
        self.window_sec = window_sec
        self._clock = clock
        self._by_ip: dict[str, deque[float]] = {}
        self._all: deque[float] = deque()

    def __call__(self, request: Request) -> None:
        ip = request.client.host if request.client else "unknown"
        retry_after = self.hit(ip)
        if retry_after is not None:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "요청이 너무 많음. 잠시 후 다시 시도",
                headers={"Retry-After": str(math.ceil(retry_after))},
            )

    def hit(self, ip: str) -> float | None:
        """허용이면 기록하고 None, 아니면 다시 시도할 수 있을 때까지 남은 초."""
        now = self._clock()
        self._prune(now)
        ip_hits = self._by_ip.get(ip, deque())
        full = [
            hits
            for hits, cap in ((ip_hits, self.per_ip), (self._all, self.total))
            if len(hits) >= cap
        ]
        if full:
            return max(hits[0] + self.window_sec - now for hits in full)
        ip_hits.append(now)
        self._by_ip[ip] = ip_hits
        self._all.append(now)
        return None

    def _prune(self, now: float) -> None:
        # 기록된 IP 수는 전체 상한 이하라 매 요청 전체를 훑어도 된다.
        cutoff = now - self.window_sec
        while self._all and self._all[0] <= cutoff:
            self._all.popleft()
        for ip in [ip for ip, hits in self._by_ip.items() if hits[-1] <= cutoff]:
            del self._by_ip[ip]
        for hits in self._by_ip.values():
            while hits[0] <= cutoff:
                hits.popleft()
