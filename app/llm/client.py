"""LLM 호출 공통 계층: 구조화 출력 파싱, 토큰 사용량 로그, 실패를 `LlmError` 하나로 통일."""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Protocol, TypeVar

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion
from pydantic import BaseModel

from app.core.config import Settings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LlmError(RuntimeError):
    """LLM 호출·파싱 실패. 호출한 서비스는 이 예외를 잡아 템플릿 경로로 대체한다."""


@dataclass(frozen=True)
class Usage:
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int


class LlmClient(Protocol):
    async def parse(self, system: str, user: str, schema: type[T]) -> tuple[T, Usage]: ...


class OpenAIClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_sec: float,
        max_retries: int,
        max_output_tokens: int,
        base_url: str | None = None,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self._model = model
        self._timeout_sec = timeout_sec
        self._max_output_tokens = max_output_tokens
        kwargs: dict[str, object] = {
            "api_key": api_key,
            "timeout": timeout_sec,
            "max_retries": max_retries,
        }
        if base_url:
            kwargs["base_url"] = base_url
        self._client = client or AsyncOpenAI(**kwargs)

    async def aclose(self) -> None:
        await self._client.close()

    async def parse(self, system: str, user: str, schema: type[T]) -> tuple[T, Usage]:
        started = time.monotonic()
        try:
            # SDK 타임아웃은 시도 1회에만 걸리고 재시도 전 retry-after 를 최대 120초 기다리므로
            # 전체 시간은 여기서 자른다.
            async with asyncio.timeout(self._timeout_sec):
                completion = await self._client.chat.completions.parse(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    response_format=schema,
                    max_completion_tokens=self._max_output_tokens,
                )
        except TimeoutError as exc:
            raise LlmError(f"LLM 호출 시간 초과 ({self._timeout_sec}초)") from exc
        except Exception as exc:
            # 200 응답 본문이 깨져도 SDK 가 OpenAIError 가 아닌 예외를 낼 수 있어 모두 잡는다.
            # 길이 초과처럼 응답을 받은 실패는 과금된 토큰을 남긴다.
            failed = getattr(exc, "completion", None)
            if isinstance(failed, ChatCompletion):
                self._log_usage(schema, _usage(failed, started), failure=type(exc).__name__)
            # 예외 메시지에 요청 본문이나 키 일부가 섞일 수 있어 종류만 남긴다.
            raise LlmError(f"LLM 호출 실패: {type(exc).__name__}") from exc

        usage = _usage(completion, started)
        self._log_usage(schema, usage)
        if not completion.choices:
            raise LlmError("LLM 응답 비어 있음")
        message = completion.choices[0].message
        if message.parsed is None:
            raise LlmError("LLM 응답 거부" if message.refusal else "LLM 응답 비어 있음")
        return message.parsed, usage

    def _log_usage(self, schema: type[BaseModel], usage: Usage, failure: str = "") -> None:
        logger.info(
            "LLM %s %s%s: 입력 %d, 출력 %d 토큰, %dms",
            schema.__name__,
            usage.model,
            f" 실패({failure})" if failure else "",
            usage.input_tokens,
            usage.output_tokens,
            usage.latency_ms,
        )


def _usage(completion: ChatCompletion, started: float) -> Usage:
    tokens = completion.usage
    return Usage(
        model=completion.model,
        input_tokens=tokens.prompt_tokens if tokens else 0,
        output_tokens=tokens.completion_tokens if tokens else 0,
        latency_ms=round((time.monotonic() - started) * 1000),
    )


_client: OpenAIClient | None = None
# 싱글톤이 붙잡은 설정. 키가 바뀌면 클라이언트를 다시 만든다.
_client_config: tuple[object, ...] | None = None


def _llm_config(settings: Settings) -> tuple[object, ...]:
    return (
        settings.openai_api_key,
        settings.openai_model,
        settings.openai_base_url or None,
        settings.llm_timeout_sec,
        settings.llm_max_retries,
        settings.llm_max_output_tokens,
    )


def _schedule_close(client: OpenAIClient) -> None:
    """동기 경로에서 이전 클라이언트를 닫는다. 실행 중 루프가 있으면 태스크로 넘긴다."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(client.aclose())


def get_openai_client(settings: Settings) -> OpenAIClient | None:
    """OPENAI_API_KEY 가 설정된 경우에만 클라이언트를 지연 생성한다.

    api_key·model·base_url·타임아웃 등이 바뀌면 이전 싱글톤을 버리고 다시 만든다.
    (Settings 자체는 get_settings lru_cache 라, 프로세스 env 만 바꾸면 재시작이 필요할 수 있다.)
    """
    global _client, _client_config
    if not settings.openai_api_key:
        return None
    config = _llm_config(settings)
    if _client is not None and _client_config == config:
        return _client
    previous = _client
    _client = OpenAIClient(
        api_key=settings.openai_api_key,
        model=settings.openai_model,
        timeout_sec=settings.llm_timeout_sec,
        max_retries=settings.llm_max_retries,
        max_output_tokens=settings.llm_max_output_tokens,
        base_url=settings.openai_base_url,
    )
    _client_config = config
    if previous is not None:
        _schedule_close(previous)
    return _client


async def close_openai_client() -> None:
    global _client, _client_config
    if _client is not None:
        await _client.aclose()
        _client = None
        _client_config = None
