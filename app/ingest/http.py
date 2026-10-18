"""공공 API 수집용 공통 HTTP 계층: 요청 간 지연, 재시도, 비밀값 마스킹."""

import asyncio
import logging
import time
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import quote, quote_plus

import httpx

logger = logging.getLogger(__name__)

# httpx 는 INFO 레벨에서 쿼리스트링(API 키 포함)이 담긴 전체 URL 을 로그로 남긴다.
logging.getLogger("httpx").setLevel(logging.WARNING)

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class IngestAPIError(RuntimeError):
    """외부 API 호출 실패. 메시지에는 API 키가 포함되지 않는다."""


def redact(text: str, secrets: Iterable[str]) -> str:
    for secret in secrets:
        if not secret:
            continue
        for form in {secret, quote(secret, safe=""), quote_plus(secret)}:
            text = text.replace(form, "***")
    return text


class ApiHttpClient:
    """요청 시작 간격을 `request_interval_sec` 이상으로 유지하고, 일시 오류는 지수 백오프로 재시도.

    재시도 대상: 연결·타임아웃 오류, 429/5xx, JSON 이 아닌 응답. 그 외 4xx 는 즉시 실패.
    """

    def __init__(
        self,
        *,
        timeout_sec: float = 30.0,
        request_interval_sec: float = 0.5,
        max_retries: int = 3,
        retry_backoff_sec: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            timeout=timeout_sec,
            transport=transport,
            headers={"User-Agent": "youthfit-ingest/0.1"},
        )
        self._request_interval_sec = request_interval_sec
        self._max_retries = max_retries
        self._retry_backoff_sec = retry_backoff_sec
        self._last_request_at: float | None = None
        self.request_count = 0

    async def __aenter__(self) -> "ApiHttpClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _throttle(self) -> None:
        if self._last_request_at is not None:
            wait = self._request_interval_sec - (time.monotonic() - self._last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
        self._last_request_at = time.monotonic()

    async def get_json(
        self,
        url: str,
        params: Mapping[str, str | int],
        *,
        secret_params: Iterable[str] = (),
    ) -> Any:
        secrets = [str(params[name]) for name in secret_params if params.get(name)]
        last_error = ""
        for attempt in range(self._max_retries + 1):
            if attempt:
                backoff = self._retry_backoff_sec * 2 ** (attempt - 1)
                logger.warning(
                    "재시도 %d/%d (%.1fs 후): %s - %s",
                    attempt,
                    self._max_retries,
                    backoff,
                    url,
                    last_error,
                )
                await asyncio.sleep(backoff)

            await self._throttle()
            self.request_count += 1
            try:
                res = await self._client.get(url, params=params)
            except httpx.TransportError as exc:
                last_error = redact(f"{type(exc).__name__}: {exc}", secrets)
                continue

            if res.status_code in RETRYABLE_STATUS:
                last_error = f"HTTP {res.status_code}"
                continue
            if res.status_code >= 400:
                raise IngestAPIError(f"{url} 요청 실패: HTTP {res.status_code}")

            try:
                return res.json()
            except ValueError:
                last_error = "JSON 이 아닌 응답"
                continue

        raise IngestAPIError(f"{url} 요청 실패 ({self._max_retries}회 재시도 후): {last_error}")
