"""The assistant's provider interface and its Groq implementation. Separate from
services/llm_provider.py, so nothing here can affect the other AI features."""

import json
import logging
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

from core.config import Settings

logger = logging.getLogger("webnest.assistant.llm")

StreamErrorCode = Literal["provider_error", "rate_limited", "invalid_tool_call"]
FinishReason = Literal["stop", "tool_calls", "length"]

_MAX_ERROR_BODY_BYTES = 4096
_CONNECT_TIMEOUT_SECONDS = 10.0
_POOL_TIMEOUT_SECONDS = 10.0
_MAX_CONNECTIONS = 50
_MAX_IDLE_CONNECTIONS = 20
_IDLE_CONNECTION_SECONDS = 60.0
_GPT_OSS_PREFIX = "openai/gpt-oss"
_QWEN_PREFIX = "qwen/"
_TOO_LARGE_CODES = frozenset({"context_length_exceeded", "request_too_large"})


@dataclass(frozen=True)
class ProviderRequest:
    """`messages` are OpenAI-style chat messages, without the system prompt."""

    system: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    max_output_tokens: int


@dataclass(frozen=True)
class StreamOpened:
    """The provider accepted the request: its status was checked, the reply is coming."""


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolCallEvent:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class UsageEvent:
    input_tokens: int
    output_tokens: int
    cached_tokens: int = 0


@dataclass(frozen=True)
class FinishedEvent:
    reason: FinishReason


ProviderEvent = StreamOpened | TextDelta | ToolCallEvent | UsageEvent | FinishedEvent


class ProviderError(Exception):
    """Base of what a provider raises. The text is ours, never the provider's."""


class ProviderRateLimitedError(ProviderError):
    def __init__(self, retry_after: int | None) -> None:
        super().__init__("provider rate limited")
        self.retry_after = retry_after


class ProviderRequestTooLargeError(ProviderError):
    pass


class ProviderUnavailableError(ProviderError):
    pass


class ProviderStreamError(ProviderError):
    """A failure after StreamOpened, with the contract's error code."""

    def __init__(self, code: StreamErrorCode) -> None:
        super().__init__(code)
        self.code: StreamErrorCode = code


class AssistantProvider(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def model(self) -> str: ...

    def stream(self, request: ProviderRequest) -> AsyncGenerator[ProviderEvent, None]:
        """Yields StreamOpened first, then the reply. Before StreamOpened a failure
        raises a ProviderError other than ProviderStreamError; after it, ProviderStreamError."""
        ...

    async def aclose(self) -> None: ...


@dataclass
class _PartialToolCall:
    id: str = ""
    name: str = ""
    arguments: str = ""


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _parse_retry_after(value: str | None) -> int | None:
    # Groq sends seconds; the HTTP-date form is treated as unknown.
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return max(int(seconds) + 1, 1) if seconds >= 0 else None


def _error_code(body: bytes) -> str:
    """Only the provider's short error code, for mapping and logs. Its message may
    quote the prompt, so it is never read or passed on."""
    try:
        parsed = json.loads(body)
    except ValueError:
        return ""
    code = _as_dict(_as_dict(parsed).get("error")).get("code")
    return code if isinstance(code, str) else ""


def _stream_error_code(error: Any) -> StreamErrorCode:
    code = _as_dict(error).get("code")
    if code == "tool_use_failed":
        return "invalid_tool_call"
    if code == "rate_limit_exceeded":
        return "rate_limited"
    return "provider_error"


def _finish_reason(raw: str, has_tool_calls: bool) -> FinishReason:
    if raw == "length":
        return "length"
    return "tool_calls" if has_tool_calls else "stop"


def _reasoning_params(model: str) -> dict[str, Any]:
    """Asks for no reasoning output, in the way each model family accepts: Groq refuses
    these parameters on models they don't apply to. Reasoning counts against the output
    limit and must never be shown, so gpt-oss is also asked to do little of it."""
    if model.startswith(_GPT_OSS_PREFIX):
        return {"include_reasoning": False, "reasoning_effort": "low"}
    if model.startswith(_QWEN_PREFIX):
        # Left to its default, this model family writes reasoning into the reply text.
        return {"reasoning_format": "hidden"}
    return {}


def _complete_tool_calls(parts: dict[int, _PartialToolCall]) -> list[ToolCallEvent]:
    """Every tool call of a finished reply, in order, or an error if any is broken."""
    calls: list[ToolCallEvent] = []
    for index in sorted(parts):
        part = parts[index]
        try:
            arguments = json.loads(part.arguments) if part.arguments.strip() else {}
        except ValueError:
            raise ProviderStreamError("invalid_tool_call") from None
        if not part.name or not isinstance(arguments, dict):
            raise ProviderStreamError("invalid_tool_call")
        calls.append(ToolCallEvent(id=part.id or f"call_{index}", name=part.name, arguments=arguments))
    return calls


def _collect_tool_call_deltas(deltas: Any, parts: dict[int, _PartialToolCall]) -> None:
    if not isinstance(deltas, list):
        return
    for position, raw in enumerate(deltas):
        delta = _as_dict(raw)
        index = delta.get("index")
        part = parts.setdefault(index if isinstance(index, int) else position, _PartialToolCall())
        function = _as_dict(delta.get("function"))
        if isinstance(delta.get("id"), str):
            part.id = delta["id"]
        if isinstance(function.get("name"), str):
            part.name += function["name"]
        if isinstance(function.get("arguments"), str):
            part.arguments += function["arguments"]


def _usage_from_chunk(chunk: dict[str, Any]) -> UsageEvent | None:
    # Where a streamed reply reports usage isn't documented, so both known places are read.
    usage = _as_dict(chunk.get("usage")) or _as_dict(_as_dict(chunk.get("x_groq")).get("usage"))
    if not usage:
        return None
    return UsageEvent(
        input_tokens=_as_int(usage.get("prompt_tokens")),
        output_tokens=_as_int(usage.get("completion_tokens")),
        cached_tokens=_as_int(_as_dict(usage.get("prompt_tokens_details")).get("cached_tokens")),
    )


class GroqAssistantProvider:
    """Streams one chat completion. One shared client keeps the connection to Groq
    alive between calls, which saves a TLS handshake on each one."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client

    @property
    def name(self) -> str:
        return "groq"

    @property
    def model(self) -> str:
        return self._settings.assistant_model

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    self._settings.assistant_timeout_seconds, connect=_CONNECT_TIMEOUT_SECONDS, pool=_POOL_TIMEOUT_SECONDS
                ),
                # Limits go on the transport: httpx ignores the client's when given a transport.
                transport=httpx.AsyncHTTPTransport(
                    retries=1,
                    limits=httpx.Limits(
                        max_connections=_MAX_CONNECTIONS,
                        max_keepalive_connections=_MAX_IDLE_CONNECTIONS,
                        keepalive_expiry=_IDLE_CONNECTION_SECONDS,
                    ),
                ),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _payload(self, request: ProviderRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": request.system}, *request.messages],
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_completion_tokens": request.max_output_tokens,
            **_reasoning_params(self.model),
        }
        if request.tools:
            payload["tools"] = request.tools
            payload["tool_choice"] = "auto"
        return payload

    async def _rejection(self, response: httpx.Response) -> ProviderError:
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) >= _MAX_ERROR_BODY_BYTES:
                break
        code = _error_code(body)
        logger.warning("Groq rejected an assistant call: status=%s code=%s", response.status_code, code)
        if response.status_code == 429:
            return ProviderRateLimitedError(_parse_retry_after(response.headers.get("retry-after")))
        if response.status_code == 413 or code in _TOO_LARGE_CODES:
            return ProviderRequestTooLargeError("request too large")
        # Includes 401/403: a bad server key must never look like the user's login failing.
        return ProviderUnavailableError(f"status {response.status_code}")

    async def stream(self, request: ProviderRequest) -> AsyncGenerator[ProviderEvent, None]:
        opened = False
        try:
            async with self._get_client().stream(
                "POST",
                f"{self._settings.assistant_groq_base_url.rstrip('/')}/chat/completions",
                json=self._payload(request),
                headers={"Authorization": f"Bearer {self._settings.assistant_groq_api_key}"},
            ) as response:
                if response.status_code != 200:
                    raise await self._rejection(response)
                opened = True
                yield StreamOpened()
                async with aclosing(self._read_events(response)) as events:
                    async for event in events:
                        yield event
        except httpx.HTTPError as exc:
            logger.warning("Groq connection failed: %s", type(exc).__name__)
            if opened:
                raise ProviderStreamError("provider_error") from exc
            raise ProviderUnavailableError("connection failed") from exc

    async def _read_events(self, response: httpx.Response) -> AsyncGenerator[ProviderEvent, None]:
        parts: dict[int, _PartialToolCall] = {}
        usage: UsageEvent | None = None
        finish: str | None = None
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if data == "[DONE]":
                # Not break: the response must be read to its end, or the connection can't
                # be reused and the next call pays for a new TLS handshake.
                continue
            try:
                chunk = _as_dict(json.loads(data))
            except ValueError:
                continue
            if "error" in chunk:
                logger.warning("Groq stream error: code=%s", _as_dict(chunk["error"]).get("code"))
                raise ProviderStreamError(_stream_error_code(chunk["error"]))
            usage = _usage_from_chunk(chunk) or usage
            choices = chunk.get("choices")
            choice = _as_dict(choices[0]) if isinstance(choices, list) and choices else {}
            # Only `content` is read: reasoning fields never reach the client.
            delta = _as_dict(choice.get("delta"))
            text = delta.get("content")
            if isinstance(text, str) and text:
                yield TextDelta(text)
            _collect_tool_call_deltas(delta.get("tool_calls"), parts)
            if isinstance(choice.get("finish_reason"), str):
                finish = choice["finish_reason"]
        # First, so a reply that fails from here on still counts what it really spent.
        if usage is not None:
            yield usage
        if finish is None:
            raise ProviderStreamError("provider_error")
        # Cut off by the output limit, a batch may be missing calls or arguments, and the
        # browser would apply only part of the plan. The finish reason tells the client.
        calls = [] if finish == "length" else _complete_tool_calls(parts)
        for call in calls:
            yield call
        yield FinishedEvent(_finish_reason(finish, bool(calls)))


def build_assistant_provider(settings: Settings) -> AssistantProvider | None:
    """The configured provider, or None for a name not supported yet (the assistant then reports 503)."""
    if settings.assistant_provider == "groq":
        return GroqAssistantProvider(settings)
    logger.error("Unknown ASSISTANT_PROVIDER: %s", settings.assistant_provider)
    return None
