import json
import logging
import time
from collections.abc import Callable

import httpx2
import pytest
from openai import AsyncOpenAI
from pydantic import BaseModel

from app.api.deps import get_llm_client
from app.core.config import Settings
from app.llm.client import (
    LlmError,
    OpenAIClient,
    close_openai_client,
    get_openai_client,
)


class Reason(BaseModel):
    plcy_no: str
    reason: str


def _completion(message: dict, *, finish_reason: str = "stop") -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-4o-mini-2024-07-18",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
    }


def _ok() -> httpx2.Response:
    content = json.dumps({"plcy_no": "P1", "reason": "서울 거주 청년 대상"})
    return httpx2.Response(200, json=_completion({"role": "assistant", "content": content}))


def _client(
    handler: Callable[[httpx2.Request], httpx2.Response],
    *,
    timeout_sec: float = 5,
    max_retries: int = 0,
) -> OpenAIClient:
    sdk = AsyncOpenAI(
        api_key="test-key",
        max_retries=max_retries,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    return OpenAIClient(
        api_key="test-key",
        model="gpt-4o-mini",
        timeout_sec=timeout_sec,
        max_retries=max_retries,
        max_output_tokens=500,
        client=sdk,
    )


async def test_parse_returns_model_and_usage():
    requests: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        return _ok()

    parsed, usage = await _client(handler).parse("시스템", "사용자", Reason)

    assert parsed == Reason(plcy_no="P1", reason="서울 거주 청년 대상")
    assert (usage.input_tokens, usage.output_tokens) == (120, 30)
    assert usage.model == "gpt-4o-mini-2024-07-18"
    body = requests[0]
    assert body["model"] == "gpt-4o-mini"
    assert body["max_completion_tokens"] == 500
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert body["response_format"]["type"] == "json_schema"


async def test_timeout_raises_llm_error():
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("timeout", request=request)

    with pytest.raises(LlmError):
        await _client(handler).parse("시스템", "사용자", Reason)


async def test_http_error_raises_llm_error():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(500, json={"error": {"message": "server error"}})

    with pytest.raises(LlmError):
        await _client(handler).parse("시스템", "사용자", Reason)


async def test_retry_succeeds_within_time_limit():
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx2.Response(500, json={"error": {"message": "server error"}})
        return _ok()

    parsed, _ = await _client(handler, max_retries=1).parse("시스템", "사용자", Reason)

    assert parsed.plcy_no == "P1"
    assert calls == 2


async def test_retry_after_wait_is_cut_by_time_limit():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429, headers={"retry-after": "60"}, json={"error": {}})

    client = _client(handler, timeout_sec=0.3, max_retries=1)
    started = time.monotonic()
    with pytest.raises(LlmError, match="시간 초과"):
        await client.parse("시스템", "사용자", Reason)
    assert time.monotonic() - started < 5


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(200, text="<html>bad gateway</html>"),
        httpx2.Response(200, text='{"choices": ['),
        httpx2.Response(200, json={**_completion({}), "choices": []}),
    ],
    ids=["html", "broken-json", "empty-choices"],
)
async def test_malformed_success_response_raises_llm_error(response: httpx2.Response):
    with pytest.raises(LlmError):
        await _client(lambda request: response).parse("시스템", "사용자", Reason)


async def test_schema_mismatch_raises_llm_error():
    def handler(request: httpx2.Request) -> httpx2.Response:
        content = json.dumps({"plcy_no": "P1"})
        return httpx2.Response(200, json=_completion({"role": "assistant", "content": content}))

    with pytest.raises(LlmError):
        await _client(handler).parse("시스템", "사용자", Reason)


async def test_refusal_raises_llm_error():
    def handler(request: httpx2.Request) -> httpx2.Response:
        message = {"role": "assistant", "content": None, "refusal": "답할 수 없습니다"}
        return httpx2.Response(200, json=_completion(message))

    with pytest.raises(LlmError, match="거부"):
        await _client(handler).parse("시스템", "사용자", Reason)


async def test_truncated_output_raises_llm_error_and_logs_usage(
    caplog: pytest.LogCaptureFixture,
):
    def handler(request: httpx2.Request) -> httpx2.Response:
        message = {"role": "assistant", "content": '{"plcy_no": "P1", "rea'}
        return httpx2.Response(200, json=_completion(message, finish_reason="length"))

    caplog.set_level(logging.INFO, logger="app.llm.client")
    with pytest.raises(LlmError):
        await _client(handler).parse("시스템", "사용자", Reason)

    assert "실패(LengthFinishReasonError)" in caplog.text
    assert "입력 120, 출력 30 토큰" in caplog.text


async def test_client_is_none_without_api_key():
    assert get_openai_client(Settings(_env_file=None, openai_api_key="")) is None


async def test_client_is_created_once_with_api_key():
    settings = Settings(_env_file=None, openai_api_key="test-key")
    try:
        first = get_openai_client(settings)
        assert first is not None
        assert get_openai_client(settings) is first
    finally:
        await close_openai_client()


async def test_client_passes_base_url():
    settings = Settings(
        _env_file=None,
        openai_api_key="test-key",
        openai_base_url="https://api.baze.co/v1/gateway",
    )
    try:
        client = get_openai_client(settings)
        assert client is not None
        assert str(client._client.base_url).rstrip("/") == "https://api.baze.co/v1/gateway"
    finally:
        await close_openai_client()


def test_tests_never_get_real_llm_client():
    assert get_llm_client() is None


def test_llm_settings_defaults(monkeypatch: pytest.MonkeyPatch):
    # 셸/프로세스에 OPENAI_* 가 있어도 기본값을 검증할 수 있게 지운다.
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)
    assert settings.openai_model == "gpt-4o-mini"
    assert settings.openai_base_url is None
    assert settings.llm_timeout_sec == 15
    assert settings.llm_max_retries == 1
    assert settings.llm_max_output_tokens == 2000
