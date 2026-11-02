"""The assistant's provider interface, with its Claude and Groq implementations. Separate
from services/llm_provider.py, so nothing here can affect the other AI features."""

import hashlib
import json
import logging
import re
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import anthropic
import httpx
import httpx2

from core.config import Settings

logger = logging.getLogger("webnest.assistant.llm")

StreamErrorCode = Literal["provider_error", "rate_limited", "invalid_tool_call", "refused"]
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
_ANTHROPIC_RETRIES = 1
_ANTHROPIC_UNSAFE_ID = re.compile(r"[^a-zA-Z0-9_-]")
_ANTHROPIC_LENGTH_STOPS = frozenset({"max_tokens", "model_context_window_exceeded"})
_HISTORY_START_TEXT = "(The start of this conversation was left out.)"
_NO_OUTPUT_TEXT = "(no output)"


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
    # The input is real but the output isn't known yet. A final usage event replaces it.
    partial: bool = False


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


def _anthropic_id(value: str) -> str:
    # Claude accepts only these characters, and history can hold ids from another provider.
    return _ANTHROPIC_UNSAFE_ID.sub("_", value)


def _anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Input streaming stays off: tool calls are held until the reply ends anyway, and this
    way the API validates each input against its schema before sending it."""
    converted: list[dict[str, Any]] = []
    for tool in tools:
        function = _as_dict(tool.get("function"))
        converted.append({"name": function.get("name"), "description": function.get("description"), "input_schema": function.get("parameters")})
    return converted


def _tool_call_input(function: dict[str, Any]) -> dict[str, Any]:
    try:
        parsed = json.loads(function.get("arguments") or "{}")
    except (ValueError, TypeError):
        return {}
    return _as_dict(parsed)


def _anthropic_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    if message["role"] == "tool":
        tool_use_id = _anthropic_id(message.get("tool_call_id") or "")
        return [{"type": "tool_result", "tool_use_id": tool_use_id, "content": message.get("content") or _NO_OUTPUT_TEXT}]
    text = message.get("content") or ""
    # Claude refuses a blank text block.
    blocks: list[dict[str, Any]] = [{"type": "text", "text": text}] if text.strip() else []
    for call in message.get("tool_calls") or []:
        function = _as_dict(call.get("function"))
        name = _anthropic_id(str(function.get("name") or ""))
        blocks.append({"type": "tool_use", "id": _anthropic_id(str(call.get("id") or "")), "name": name, "input": _tool_call_input(function)})
    return blocks


def anthropic_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Our OpenAI-style history in Claude's format: tool calls and results become content
    blocks, and neighbouring messages of one role merge, as tool results must share a turn."""
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = "user" if message["role"] == "tool" else message["role"]
        blocks = _anthropic_blocks(message)
        if not blocks:
            continue
        if converted and converted[-1]["role"] == role:
            converted[-1]["content"].extend(blocks)
        else:
            converted.append({"role": role, "content": blocks})
    if not converted or converted[0]["role"] != "user":
        converted.insert(0, {"role": "user", "content": [{"type": "text", "text": _HISTORY_START_TEXT}]})
    return converted


@dataclass
class _AnthropicReply:
    """What a streamed reply has said so far."""

    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    has_usage: bool = False
    stop_reason: str | None = None

    def __post_init__(self) -> None:
        self.parts: dict[int, _PartialToolCall] = {}

    def note_usage(self, usage: Any) -> None:
        """Every field that is billed. Input is the three kinds together: `input_tokens`
        alone leaves out what was written to or read from the cache."""
        if usage is None:
            return
        fresh = _as_int(getattr(usage, "input_tokens", None))
        written = _as_int(getattr(usage, "cache_creation_input_tokens", None))
        read = _as_int(getattr(usage, "cache_read_input_tokens", None))
        if fresh or written or read:
            self.input_tokens = fresh + written + read
            self.cached_tokens = read
        # Cumulative, and it includes any thinking.
        self.output_tokens = max(self.output_tokens, _as_int(getattr(usage, "output_tokens", None)))
        self.has_usage = True

    def read(self, event: Any) -> str | None:
        """Takes in one stream event, and returns its text if it has any to show. Only text
        blocks are shown, so thinking could never reach the client."""
        kind = event.type
        if kind == "message_start":
            self.note_usage(event.message.usage)
        elif kind == "content_block_start":
            block = event.content_block
            if block.type == "tool_use":
                self.parts[event.index] = _PartialToolCall(id=_public_call_id(block.id), name=block.name)
        elif kind == "content_block_delta":
            delta = event.delta
            if delta.type == "text_delta":
                return delta.text or None
            if delta.type == "input_json_delta" and event.index in self.parts:
                self.parts[event.index].arguments += delta.partial_json
        elif kind == "message_delta":
            self.note_usage(event.usage)
            self.stop_reason = event.delta.stop_reason
        return None

    def usage(self, partial: bool = False) -> UsageEvent:
        return UsageEvent(self.input_tokens, self.output_tokens, self.cached_tokens, partial)


def _public_call_id(provider_id: str) -> str:
    """Clients see and send back this id, and Claude's own has a telltale prefix. Claude only
    needs a call and its result to share an id, so one that hides where it came from works."""
    return f"call_{hashlib.sha256(provider_id.encode('utf-8')).hexdigest()[:24]}"


def _anthropic_rejection(exc: anthropic.APIStatusError) -> ProviderError:
    """Our own errors from the status alone. The API's message can quote the prompt, so
    only its status and short type reach the logs. Running out of credit lands here too."""
    logger.warning("Claude rejected an assistant call: status=%s type=%s", exc.status_code, getattr(exc, "type", None))
    if exc.status_code == 429:
        return ProviderRateLimitedError(_parse_retry_after(exc.response.headers.get("retry-after")))
    if exc.status_code == 413:
        return ProviderRequestTooLargeError("request too large")
    # Includes 401/403: a bad server key must never look like the user's login failing.
    return ProviderUnavailableError(f"status {exc.status_code}")


class AnthropicAssistantProvider:
    """Streams one Claude message through the official SDK. One shared client keeps its
    connection alive between calls. StreamOpened waits for the stream's first event, because
    Claude can report overload as a stream event: failing that early can still fall back."""

    def __init__(self, settings: Settings, client: anthropic.AsyncAnthropic | None = None) -> None:
        self._settings = settings
        self._client = client

    @property
    def name(self) -> str:
        return "anthropic"

    @property
    def model(self) -> str:
        return self._settings.assistant_anthropic_model

    def _get_client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            self._client = anthropic.AsyncAnthropic(
                api_key=self._settings.assistant_anthropic_api_key,
                max_retries=_ANTHROPIC_RETRIES,
                timeout=anthropic.Timeout(self._settings.assistant_timeout_seconds, connect=_CONNECT_TIMEOUT_SECONDS),
            )
        return self._client

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.close()

    def params(self, request: ProviderRequest) -> dict[str, Any]:
        """Thinking is off: it is billed as output, and the optimizer rewrites history, which
        replayed thinking blocks wouldn't survive. No sampling settings, which Haiku 5.5 refuses."""
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_output_tokens,
            "system": request.system,
            "messages": anthropic_messages(request.messages),
            "thinking": {"type": "disabled"},
            "stream": True,
        }
        if request.tools:
            params["tools"] = _anthropic_tools(request.tools)
        return params

    async def stream(self, request: ProviderRequest) -> AsyncGenerator[ProviderEvent, None]:
        reply = _AnthropicReply()
        opened = False
        try:
            async with await self._get_client().messages.create(**self.params(request)) as events:
                async for event in events:
                    if not opened and event.type == "message_start":
                        opened = True
                        yield StreamOpened()
                    text = reply.read(event)
                    if text:
                        yield TextDelta(text)
                    if event.type == "message_start":
                        # At once, so a client that leaves now is charged the real input.
                        yield reply.usage(partial=True)
        except anthropic.APIStatusError as exc:
            failure = _anthropic_rejection(exc)
            if not opened:
                raise failure from exc
            # What it spent still counts, so the usage goes first.
            yield reply.usage()
            raise ProviderStreamError("rate_limited" if exc.status_code == 429 else "provider_error") from exc
        except (anthropic.APIError, httpx2.HTTPError) as exc:
            logger.warning("Claude connection failed: %s", type(exc).__name__)
            if not opened:
                raise ProviderUnavailableError("connection failed") from exc
            yield reply.usage()
            raise ProviderStreamError("provider_error") from exc
        if not opened:
            raise ProviderUnavailableError("empty stream")
        if reply.has_usage:
            yield reply.usage()
        for event in self._ending(reply):
            yield event

    def _ending(self, reply: _AnthropicReply) -> list[ProviderEvent]:
        if reply.stop_reason is None:
            raise ProviderStreamError("provider_error")
        if reply.stop_reason == "refusal":
            logger.warning("Claude declined an assistant call")
            raise ProviderStreamError("refused")
        # Cut off by the output limit, a call's input may be partial and still parse, and the
        # browser would run it. The finish reason tells the client instead.
        if reply.stop_reason in _ANTHROPIC_LENGTH_STOPS:
            return [FinishedEvent("length")]
        calls = _complete_tool_calls(reply.parts)
        return [*calls, FinishedEvent("tool_calls" if calls else "stop")]


def provider_has_key(settings: Settings, name: str) -> bool:
    if name == "anthropic":
        return bool(settings.assistant_anthropic_api_key)
    return bool(settings.assistant_groq_api_key)


def assistant_can_answer(settings: Settings) -> bool:
    """Whether the assistant is switched on with a provider that has its key."""
    if not settings.assistant_enabled:
        return False
    if settings.assistant_provider == "anthropic":
        return bool(settings.assistant_anthropic_api_key or settings.assistant_groq_api_key)
    return settings.assistant_provider == "groq" and bool(settings.assistant_groq_api_key)


def build_assistant_provider(settings: Settings) -> AssistantProvider | None:
    """The provider to try first, or None for an unknown name (the assistant then reports 503).
    With no Claude key this stays on Groq, so deploying before the key is added changes nothing."""
    if settings.assistant_provider == "anthropic":
        if settings.assistant_anthropic_api_key:
            return AnthropicAssistantProvider(settings)
        logger.warning("No Claude API key is set, so the assistant is using Groq")
        return GroqAssistantProvider(settings)
    if settings.assistant_provider == "groq":
        return GroqAssistantProvider(settings)
    logger.error("Unknown ASSISTANT_PROVIDER: %s", settings.assistant_provider)
    return None


def build_assistant_fallback(settings: Settings) -> AssistantProvider | None:
    """Groq behind Claude, when both have keys. Nothing goes behind Groq: an outage there
    must not start spending Claude credit when Groq-only was chosen."""
    if settings.assistant_provider == "anthropic" and settings.assistant_anthropic_api_key and settings.assistant_groq_api_key:
        return GroqAssistantProvider(settings)
    return None
