"""The assistant's request flow: limits, prompt, opening the provider, streaming
the reply and recording usage. It runs no user code and stores no chats."""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing
from datetime import date, datetime, timedelta, timezone
from typing import Any

from core.config import Settings
from core.exceptions import GenerationUnavailableError, PayloadTooLargeError, RateLimitedError
from database.assistant_usage_persistence import AssistantUsageStore, DailyLimits, TokenCounts, UsageKey
from schemas.assistant_schemas import AssistantUsageResponse, ChatRequest
from services.assistant_llm import (
    AssistantProvider,
    FinishedEvent,
    FinishReason,
    ProviderError,
    ProviderEvent,
    ProviderRateLimitedError,
    ProviderRequest,
    ProviderRequestTooLargeError,
    ProviderStreamError,
    StreamErrorCode,
    StreamOpened,
    TextDelta,
    ToolCallEvent,
    UsageEvent,
)
from services.assistant_prompt_optimizer import CHARS_PER_TOKEN, optimize_conversation
from services.assistant_prompts import prefix_chars, system_prompt, tool_names, tools_for

logger = logging.getLogger("webnest.assistant")

PING_INTERVAL_SECONDS = 15.0
# The longest a reply may stay open, whatever the client or Groq do. Far beyond any real
# reply: output is capped at ASSISTANT_MAX_OUTPUT_TOKENS.
MAX_STREAM_SECONDS = 600.0
_EXPIRY_MARGIN_SECONDS = 30.0
_QUEUE_SIZE = 64
_PUMP_STOP_TIMEOUT_SECONDS = 5.0
_UNAVAILABLE_MESSAGE = "The AI assistant is not available right now. Please try again later."
_STREAM_ERROR_MESSAGES: dict[StreamErrorCode, str] = {
    "provider_error": "The AI service stopped responding. Please try again.",
    "rate_limited": "The AI service is busy. Please try again in a moment.",
    "invalid_tool_call": "The AI returned an invalid tool call. Please try again.",
}


class AssistantRateLimitedError(RateLimitedError):
    def __init__(self, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ActiveStreamRegistry:
    """Users with a reply streaming in this process. Used on the event loop only,
    with no await between check and set, so it needs no lock."""

    def __init__(self) -> None:
        self._users: set[uuid.UUID] = set()

    def acquire(self, user_id: uuid.UUID) -> bool:
        if user_id in self._users:
            return False
        self._users.add(user_id)
        return True

    def release(self, user_id: uuid.UUID) -> None:
        self._users.discard(user_id)


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _seconds_until_next_utc_day() -> int:
    now = datetime.now(timezone.utc)
    midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    return max(int((midnight - now).total_seconds()) + 1, 1)


def _sse(event: str, data: dict[str, Any]) -> bytes:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8", "replace")


class AssistantChatStream:
    """One opened model reply. `events()` yields the SSE bytes; `close()` must run
    however the request ended. It stops the provider and settles usage, once. If nothing
    ever calls it, the expiry timer does, so no connection or task can outlive the limit."""

    def __init__(
        self,
        key: UsageKey,
        reserved: TokenCounts,
        store: AssistantUsageStore,
        registry: ActiveStreamRegistry,
        queue: asyncio.Queue[ProviderEvent | ProviderError | None],
        pump: asyncio.Task[None],
        allowed_tools: frozenset[str],
        calls_used_today: int,
        calls_limit: int,
        estimated_input_tokens: int,
        original_chars: int,
        optimized_chars: int,
        started_at: float,
        ping_interval: float,
        max_seconds: float,
    ) -> None:
        self._key = key
        self._reserved = reserved
        self._store = store
        self._registry = registry
        self._queue = queue
        self._pump = pump
        self._allowed_tools = allowed_tools
        self._calls_used_today = calls_used_today
        self._calls_limit = calls_limit
        self._estimated_input_tokens = estimated_input_tokens
        self._original_chars = original_chars
        self._optimized_chars = optimized_chars
        self._started_at = started_at
        self._ping_interval = ping_interval
        self._max_seconds = max_seconds
        self._usage: UsageEvent | None = None
        self._output_chars = 0
        self._first_output_ms: int | None = None
        self._outcome = "disconnected"
        self._closed = False
        self._slot_released = False
        self._expiry_task: asyncio.Task[None] | None = None
        # After the response's own limit, so this only acts if the response never ran.
        self._expiry = asyncio.get_running_loop().call_later(max_seconds + _EXPIRY_MARGIN_SECONDS, self._expire)

    @property
    def max_seconds(self) -> float:
        return self._max_seconds

    async def events(self) -> AsyncIterator[bytes]:
        finish: FinishReason | None = None
        pending_calls: list[ToolCallEvent] = []
        try:
            while True:
                try:
                    item = await asyncio.wait_for(self._queue.get(), self._ping_interval)
                except TimeoutError:
                    yield b": ping\n\n"
                    continue
                if item is None:
                    break
                if isinstance(item, ProviderError):
                    yield self._error_event(item.code if isinstance(item, ProviderStreamError) else "provider_error")
                    return
                if isinstance(item, TextDelta):
                    self._note_output(len(item.text))
                    yield _sse("text", {"delta": item.text})
                elif isinstance(item, ToolCallEvent):
                    self._note_output(len(json.dumps(item.arguments)))
                    pending_calls.append(item)
                elif isinstance(item, UsageEvent):
                    self._usage = item
                elif isinstance(item, FinishedEvent):
                    # All or none: the browser runs each call it gets, so it must not
                    # delete a file for a reply that then fails on a later call.
                    if any(call.name not in self._allowed_tools for call in pending_calls):
                        yield self._error_event("invalid_tool_call")
                        return
                    for call in pending_calls:
                        yield _sse("tool_call", {"id": call.id, "name": call.name, "arguments": call.arguments})
                    pending_calls = []
                    finish = item.reason
            if finish is None:
                yield self._error_event("provider_error")
                return
            actual = self._tokens()
            self._outcome = "completed"
            self._release_slot()
            yield _sse(
                "usage",
                {
                    "input_tokens": actual.input_tokens,
                    "output_tokens": actual.output_tokens,
                    "calls_used_today": self._calls_used_today,
                    "calls_limit": self._calls_limit,
                },
            )
            yield _sse("done", {"finish_reason": finish})
        except Exception as exc:
            # Headers are long gone, so the contract's error event is the only way to report it.
            logger.error("Assistant stream failed: %s", type(exc).__name__)
            yield self._error_event("provider_error")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._expiry.cancel()
        self._pump.cancel()
        # Waits for the connection to Groq to close, so nobody pays for tokens nobody sees.
        _, still_running = await asyncio.wait({self._pump}, timeout=_PUMP_STOP_TIMEOUT_SECONDS)
        if still_running:
            logger.error("Assistant provider stream did not stop in time")
        self._release_slot()
        actual = self._tokens()
        try:
            await self._store.settle_tokens(self._key, self._reserved, actual)
        except Exception as exc:
            logger.error("Could not record assistant usage: %s", type(exc).__name__)
        logger.info(
            "Assistant call: user=%s model=%s outcome=%s input_tokens=%d output_tokens=%d exact_usage=%s "
            "first_output_ms=%s duration_ms=%d request_chars=%d sent_chars=%d",
            self._key.user_id,
            self._key.model,
            self._outcome,
            actual.input_tokens,
            actual.output_tokens,
            self._usage is not None,
            self._first_output_ms,
            int((time.perf_counter() - self._started_at) * 1000),
            self._original_chars,
            self._optimized_chars,
        )

    def _expire(self) -> None:
        logger.error("Assistant stream hit its time limit: user=%s", self._key.user_id)
        self._expiry_task = asyncio.create_task(self.close())

    def _release_slot(self) -> None:
        """Frees the user's stream slot, once: a later release from this stream must not
        free the slot of the user's next stream. Done before the client is told the reply
        ended, because an agent's next call follows immediately."""
        if not self._slot_released:
            self._slot_released = True
            self._registry.release(self._key.user_id)

    def _note_output(self, chars: int) -> None:
        self._output_chars += chars
        if self._first_output_ms is None:
            self._first_output_ms = int((time.perf_counter() - self._started_at) * 1000)

    def _tokens(self) -> TokenCounts:
        """The provider's numbers, or an estimate when the stream ended before it sent
        them, so a disconnect still counts towards the global cap."""
        if self._usage is not None:
            return TokenCounts(self._usage.input_tokens, self._usage.output_tokens)
        return TokenCounts(self._estimated_input_tokens, self._output_chars // CHARS_PER_TOKEN)

    def _error_event(self, code: StreamErrorCode) -> bytes:
        self._outcome = code
        self._release_slot()
        return _sse("error", {"code": code, "message": _STREAM_ERROR_MESSAGES[code]})


async def _pump_provider(
    stream: AsyncGenerator[ProviderEvent, None], queue: asyncio.Queue[ProviderEvent | ProviderError | None]
) -> None:
    """Owns the provider stream, so one task opens and closes the connection. Cancelling
    it closes the stream even while waiting on the queue, which stops the spending."""
    try:
        async with aclosing(stream) as events:
            async for event in events:
                await queue.put(event)
        await queue.put(None)
    except ProviderError as exc:
        await queue.put(exc)
    except Exception as exc:
        logger.error("Assistant provider failed: %s", type(exc).__name__)
        await queue.put(ProviderStreamError("provider_error"))


class AssistantService:
    def __init__(
        self,
        settings: Settings,
        provider: AssistantProvider | None,
        store: AssistantUsageStore,
        registry: ActiveStreamRegistry,
        ping_interval: float = PING_INTERVAL_SECONDS,
        max_stream_seconds: float = MAX_STREAM_SECONDS,
    ) -> None:
        self._settings = settings
        self._provider = provider
        self._store = store
        self._registry = registry
        self._ping_interval = ping_interval
        self._max_stream_seconds = max_stream_seconds

    def is_enabled(self) -> bool:
        return bool(self._settings.assistant_enabled and self._settings.assistant_groq_api_key and self._provider is not None)

    def ensure_enabled(self) -> None:
        if not self.is_enabled():
            raise GenerationUnavailableError("The AI assistant is switched off.")

    async def get_usage(self, user_id: uuid.UUID) -> AssistantUsageResponse:
        return AssistantUsageResponse(
            enabled=self.is_enabled(),
            calls_used_today=await self._store.calls_today(user_id, _utc_today()),
            calls_limit=self._settings.assistant_user_daily_calls,
        )

    def build_request(self, payload: ChatRequest) -> tuple[ProviderRequest, int, int, int]:
        """The provider request, built only from server-side choices and the checked
        payload, with the optimizer's estimated tokens and its before and after sizes."""
        language = payload.context.language
        system = system_prompt(payload.mode, language)
        tools = tools_for(payload.mode, language)
        prefix_tokens = prefix_chars(system, tools) // CHARS_PER_TOKEN
        optimized = optimize_conversation(
            payload.messages, payload.context, payload.mode, self._settings.assistant_max_input_tokens, prefix_tokens
        )
        request = ProviderRequest(
            system=system,
            messages=optimized.messages,
            tools=tools,
            max_output_tokens=self._settings.assistant_max_output_tokens,
        )
        return request, optimized.estimated_tokens + prefix_tokens, optimized.original_chars, optimized.optimized_chars

    async def open_chat(self, user_id: uuid.UUID, payload: ChatRequest) -> AssistantChatStream:
        """Checks limits and opens the provider, so its refusals become real HTTP statuses.
        On success the caller must eventually call close() on the returned stream."""
        self.ensure_enabled()
        provider = self._provider
        if provider is None:
            raise GenerationUnavailableError(_UNAVAILABLE_MESSAGE)
        # Checked first because it needs no database, so a double-send costs nothing.
        if not self._registry.acquire(user_id):
            raise AssistantRateLimitedError("Another reply is still streaming. Wait for it to finish.")
        try:
            return await self._open_reserved(user_id, payload, provider)
        except BaseException:
            self._registry.release(user_id)
            raise

    async def _open_reserved(self, user_id: uuid.UUID, payload: ChatRequest, provider: AssistantProvider) -> AssistantChatStream:
        started_at = time.perf_counter()
        key = UsageKey(user_id=user_id, day=_utc_today(), provider=provider.name, model=provider.model)
        # Built first: nothing here can fail after a call has been counted.
        request, estimated_tokens, original_chars, optimized_chars = self.build_request(payload)
        # The most this call can spend, so it counts towards the caps while in flight.
        reserved = TokenCounts(estimated_tokens, request.max_output_tokens)
        limits = DailyLimits(
            user_calls=self._settings.assistant_user_daily_calls,
            user_tokens=self._settings.assistant_user_daily_tokens,
            global_tokens=self._settings.assistant_global_daily_tokens,
        )
        reservation = await self._store.reserve_call(key, limits, reserved)
        if reservation.denied == "global_tokens":
            raise GenerationUnavailableError("The AI assistant has reached its daily limit. Please try again tomorrow.")
        if reservation.denied == "user_calls":
            raise AssistantRateLimitedError(
                f"You have used all {self._settings.assistant_user_daily_calls} AI assistant calls for today.",
                retry_after=_seconds_until_next_utc_day(),
            )
        if reservation.denied == "user_tokens":
            raise AssistantRateLimitedError(
                "You have used your AI assistant allowance for today.",
                retry_after=_seconds_until_next_utc_day(),
            )

        queue: asyncio.Queue[ProviderEvent | ProviderError | None] = asyncio.Queue(maxsize=_QUEUE_SIZE)
        pump = asyncio.create_task(_pump_provider(provider.stream(request), queue))
        try:
            first = await queue.get()
        except BaseException:
            pump.cancel()
            raise
        if not isinstance(first, StreamOpened):
            # Normally already finished; this covers a provider that broke its contract.
            pump.cancel()
            await self._refund(key, reserved)
            raise self._pre_stream_error(first)
        return AssistantChatStream(
            key=key,
            reserved=reserved,
            store=self._store,
            registry=self._registry,
            queue=queue,
            pump=pump,
            allowed_tools=tool_names(request.tools),
            calls_used_today=reservation.calls_today,
            calls_limit=self._settings.assistant_user_daily_calls,
            estimated_input_tokens=estimated_tokens,
            original_chars=original_chars,
            optimized_chars=optimized_chars,
            started_at=started_at,
            ping_interval=self._ping_interval,
            max_seconds=self._max_stream_seconds,
        )

    async def _refund(self, key: UsageKey, reserved: TokenCounts) -> None:
        # A provider outage must not use up the user's daily calls.
        try:
            await self._store.release_call(key, reserved)
        except Exception as exc:
            logger.error("Could not give back an assistant call: %s", type(exc).__name__)

    def _pre_stream_error(self, failure: ProviderEvent | ProviderError | None) -> Exception:
        if isinstance(failure, ProviderRateLimitedError):
            return AssistantRateLimitedError("The AI service is busy. Please try again in a moment.", failure.retry_after)
        if isinstance(failure, ProviderRequestTooLargeError):
            return PayloadTooLargeError("This conversation is too large. Please start a new chat.")
        return GenerationUnavailableError(_UNAVAILABLE_MESSAGE)
