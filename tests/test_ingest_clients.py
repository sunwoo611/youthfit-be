import json
import logging
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from app.core.config import Settings
from app.ingest.http import ApiHttpClient, IngestAPIError, redact
from app.ingest.ontong import OntongClient

SAMPLES = Path(__file__).resolve().parents[1] / "docs" / "data-samples"
API_KEY = "test-secret-key"


def _http(handler: Callable[[httpx.Request], httpx.Response]) -> ApiHttpClient:
    return ApiHttpClient(
        request_interval_sec=0,
        retry_backoff_sec=0,
        transport=httpx.MockTransport(handler),
    )


def _ontong_body(policies: list[dict], total: int, page: int, size: int) -> dict:
    return {
        "resultCode": 200,
        "resultMessage": "성공적으로 데이터를 가지고 왔습니다.",
        "result": {
            "pagging": {"totCount": total, "pageNum": page, "pageSize": size},
            "youthPolicyList": policies,
        },
    }


async def test_ontong_paginates_until_total_count():
    requests: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        page = int(req.url.params["pageNum"])
        policies = [{"plcyNo": f"P{page}-{i}"} for i in range(2 if page < 3 else 1)]
        return httpx.Response(200, json=_ontong_body(policies, total=5, page=page, size=2))

    async with _http(handler) as http:
        result = await OntongClient(API_KEY, http, page_size=2).fetch_all_policies()

    assert [p["plcyNo"] for p in result.policies] == ["P1-0", "P1-1", "P2-0", "P2-1", "P3-0"]
    assert result.total_count == 5
    assert [r.url.params["pageNum"] for r in requests] == ["1", "2", "3"]
    first = requests[0].url.params
    assert first["apiKeyNm"] == API_KEY
    assert first["pageSize"] == "2"
    assert first["rtnType"] == "json"


async def test_ontong_parses_sample_response():
    sample = json.loads((SAMPLES / "ontong-getPlcy-sample.json").read_text(encoding="utf-8"))

    async with _http(lambda req: httpx.Response(200, json=sample)) as http:
        page = await OntongClient(API_KEY, http, page_size=3).fetch_page(1)

    assert page.total_count == 3264
    assert len(page.policies) == 3


async def test_ontong_stops_on_empty_page():
    def handler(req: httpx.Request) -> httpx.Response:
        page = int(req.url.params["pageNum"])
        policies = [{"plcyNo": "P1"}] if page == 1 else []
        return httpx.Response(200, json=_ontong_body(policies, total=10, page=page, size=1))

    async with _http(handler) as http:
        result = await OntongClient(API_KEY, http, page_size=1).fetch_all_policies()

    assert [p["plcyNo"] for p in result.policies] == ["P1"]
    assert result.total_count == 10


async def test_ontong_keeps_duplicates_for_caller():
    def handler(req: httpx.Request) -> httpx.Response:
        page = int(req.url.params["pageNum"])
        policies = [{"plcyNo": "A"}, {"plcyNo": "B"}] if page == 1 else [{"plcyNo": "B"}]
        return httpx.Response(200, json=_ontong_body(policies, total=3, page=page, size=2))

    async with _http(handler) as http:
        result = await OntongClient(API_KEY, http, page_size=2).fetch_all_policies()

    assert [p["plcyNo"] for p in result.policies] == ["A", "B", "B"]
    assert result.total_count == 3


async def test_ontong_error_result_code_raises():
    body = {"resultCode": 401, "resultMessage": "인증키 오류"}

    async with _http(lambda req: httpx.Response(200, json=body)) as http:
        with pytest.raises(IngestAPIError, match="resultCode=401"):
            await OntongClient(API_KEY, http).fetch_page(1)


async def test_retries_transient_errors_then_succeeds():
    responses = iter(
        [
            httpx.Response(500),
            httpx.Response(200, text="<html>점검 중</html>"),
            httpx.Response(200, json=_ontong_body([{"plcyNo": "P1"}], total=1, page=1, size=100)),
        ]
    )
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return next(responses)

    async with _http(handler) as http:
        result = await OntongClient(API_KEY, http).fetch_all_policies()

    assert calls == 3
    assert http.request_count == 3
    assert [p["plcyNo"] for p in result.policies] == ["P1"]


async def test_retries_transport_error():
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectTimeout(f"timeout for {req.url}", request=req)
        return httpx.Response(200, json=_ontong_body([], total=0, page=1, size=100))

    async with _http(handler) as http:
        result = await OntongClient(API_KEY, http).fetch_all_policies()
        assert result.policies == []
        assert result.total_count == 0

    assert calls == 2


async def test_gives_up_after_max_retries_without_leaking_key(caplog):
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError(f"cannot connect {req.url}", request=req)

    caplog.set_level(logging.DEBUG)
    async with _http(handler) as http:
        with pytest.raises(IngestAPIError) as exc_info:
            await OntongClient(API_KEY, http).fetch_page(1)

    assert calls == 4
    assert "3회 재시도" in str(exc_info.value)
    assert API_KEY not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert API_KEY not in caplog.text


def test_redact_masks_url_encoded_key():
    key = "ab+c/d=="
    text = f"raw={key} quoted=ab%2Bc%2Fd%3D%3D"
    assert redact(text, [key]) == "raw=*** quoted=***"


async def test_client_error_is_not_retried():
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404)

    async with _http(handler) as http:
        with pytest.raises(IngestAPIError, match="HTTP 404"):
            await OntongClient(API_KEY, http).fetch_page(1)

    assert calls == 1


async def test_request_interval_is_applied(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(sec: float) -> None:
        sleeps.append(sec)

    monkeypatch.setattr("app.ingest.http.asyncio.sleep", fake_sleep)
    body = _ontong_body([{"plcyNo": "P"}], total=3, page=1, size=1)
    http = ApiHttpClient(
        request_interval_sec=0.5,
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body)),
    )
    async with http:
        await OntongClient(API_KEY, http, page_size=1).fetch_all_policies()

    assert http.request_count == 3
    assert len(sleeps) == 2
    assert all(0 < s <= 0.5 for s in sleeps)


async def test_from_settings_requires_api_key():
    settings = Settings(_env_file=None, ontong_youth_api_key=None)
    async with _http(lambda req: httpx.Response(200)) as http:
        with pytest.raises(ValueError, match="ONTONG_YOUTH_API_KEY"):
            OntongClient.from_settings(settings, http)
