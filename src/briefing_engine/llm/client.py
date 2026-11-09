"""The only module that imports the Anthropic SDK (§7.2).

Everything else depends on `StructuredClient`, so tests can substitute a fake.

- No sampling parameters (`temperature`, `top_p`, `top_k`): current models reject them,
  which was Horizon bug #1.
- Effort is set explicitly on every call, because the default differs between models.
- The SDK's own retries are off. Retries go through the job queue, where attempts,
  backoff and cost are all visible.
- Errors are classified by exception type and status code, never by message text.
"""

import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, Protocol

import anthropic
from anthropic.types import MessageParam, TextBlockParam

from briefing_engine.llm.pricing import TokenUsage

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class ErrorKind(StrEnum):
    RETRYABLE = "retryable"  # rate limits, overload, 5xx, timeouts, connection failures
    FATAL_REQUEST = "fatal_request"  # a bug in the request: fix the code
    FATAL_AUTH = "fatal_auth"  # bad or unauthorised key: nothing will work until fixed


class LlmApiError(Exception):
    def __init__(
        self,
        kind: ErrorKind,
        message: str,
        *,
        status_code: int | None = None,
        request_id: str | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code
        self.request_id = request_id
        self.retry_after_s = retry_after_s

    def __str__(self) -> str:
        status = f" {self.status_code}" if self.status_code else ""
        return f"{self.kind}{status}: {self.args[0]}"


@dataclass(frozen=True)
class ModelReply:
    text: str | None
    stop_reason: str | None
    usage: TokenUsage
    request_id: str | None
    latency_ms: int


class StructuredClient(Protocol):
    def create_structured(
        self,
        *,
        model: str,
        system: str,
        cache_system: bool,
        user: str,
        json_schema: dict[str, Any],
        max_tokens: int,
        effort: Effort,
    ) -> ModelReply: ...


def _retry_after(exc: anthropic.APIStatusError) -> float | None:
    value = exc.response.headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def classify_api_error(exc: anthropic.APIError) -> LlmApiError:
    if isinstance(exc, anthropic.APIStatusError):
        common: dict[str, Any] = {"status_code": exc.status_code, "request_id": exc.request_id}
        name = type(exc).__name__
        if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
            return LlmApiError(ErrorKind.FATAL_AUTH, name, **common)
        if isinstance(exc, anthropic.RateLimitError):
            return LlmApiError(ErrorKind.RETRYABLE, name, retry_after_s=_retry_after(exc), **common)
        if exc.status_code >= 500 or exc.status_code in (408, 409):
            return LlmApiError(ErrorKind.RETRYABLE, name, retry_after_s=_retry_after(exc), **common)
        return LlmApiError(ErrorKind.FATAL_REQUEST, name, **common)
    if isinstance(exc, anthropic.APIConnectionError):  # includes APITimeoutError
        return LlmApiError(ErrorKind.RETRYABLE, type(exc).__name__)
    return LlmApiError(ErrorKind.FATAL_REQUEST, type(exc).__name__)


class AnthropicClient:
    def __init__(self, api_key: str, base_url: str | None = None, timeout_s: float = 60.0):
        self._client = anthropic.Anthropic(
            api_key=api_key, base_url=base_url, max_retries=0, timeout=timeout_s
        )

    def create_structured(
        self,
        *,
        model: str,
        system: str,
        cache_system: bool,
        user: str,
        json_schema: dict[str, Any],
        max_tokens: int,
        effort: Effort,
    ) -> ModelReply:
        system_block: TextBlockParam = {"type": "text", "text": system}
        if cache_system:
            system_block["cache_control"] = {"type": "ephemeral"}
        messages: list[MessageParam] = [{"role": "user", "content": user}]
        started = time.monotonic()
        try:
            message = self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=[system_block],
                messages=messages,
                output_config={
                    "effort": effort,
                    "format": {"type": "json_schema", "schema": json_schema},
                },
            )
        except anthropic.APIError as exc:
            raise classify_api_error(exc) from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        text = next((block.text for block in message.content if block.type == "text"), None)
        usage = message.usage
        return ModelReply(
            text=text,
            stop_reason=message.stop_reason,
            usage=TokenUsage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_input_tokens or 0,
                cache_write_tokens=usage.cache_creation_input_tokens or 0,
            ),
            request_id=message._request_id,
            latency_ms=latency_ms,
        )
