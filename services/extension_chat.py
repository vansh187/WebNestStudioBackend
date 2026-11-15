"""WebNest AI for VS Code on top of the existing CodeLab assistant.

A VS Code request becomes an ordinary ChatRequest (language "vscode", Ask mode) and goes
through AssistantService.open_chat unchanged, so the prompt optimizer, the Prime pass, the
daily and weekly limits shared with the website and Android, the provider fallback, the
one-stream-per-user rule and the call log all apply. Nothing here calls the AI provider
itself. The assistant's refusals become the contract's coded errors, and each is logged with
the user id and the code only, never the request."""

import asyncio
import json
import logging
import secrets
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar

from pydantic import ValidationError as PydanticValidationError

from core.config import Settings
from core.exceptions import (
    DatabaseError,
    DomainError,
    ExtensionError,
    GenerationUnavailableError,
    PayloadTooLargeError,
    PaymentRequiredError,
)
from database.assistant_usage_persistence import AssistantUsageStore, TokenTotals
from database.extension_session_persistence import SessionUser
from schemas.assistant_schemas import (
    MAX_ANY_QUESTION_CHARS,
    MAX_CONTEXT_FILES,
    VSCODE_LANGUAGE,
    AccessResponse,
    ChatContext,
    ChatMessage,
    ChatRequest,
    ChatToolCall,
    OpenFile,
)
from schemas.extension_schemas import (
    MAX_AGENT_STEPS,
    MAX_ATTACHMENTS,
    MAX_BODY_BYTES,
    MAX_MESSAGE_CHARS,
    MAX_TOOL_RESULT_CHARS,
    ActiveFile,
    Cap,
    Diagnostic,
    ExtContext,
    ExtensionChatRequest,
    ExtMessage,
    ExtToolCall,
    MeAccess,
    MeExtension,
    MeLimits,
    MeLinks,
    MeResponse,
    MeService,
    MeUser,
    one_line,
)
from services.assistant_payment_service import AssistantPaymentService
from services.assistant_prompt_optimizer import CHARS_PER_TOKEN, cut_middle
from services.assistant_service import AssistantChatStream, AssistantRateLimitedError, AssistantService

logger = logging.getLogger("webnest.extension.chat")

PROVIDER_NAME = "WebNest AI"
MAX_KEPT_MESSAGES = 60
MAX_PROBLEM_CHARS = 20_000
_PROVIDER_BUSY_RETRY_SECONDS = 5
_UNAVAILABLE_RETRY_SECONDS = 60
_DATABASE_RETRY_SECONDS = 5
_MAX_NOTE_PATH_CHARS = 300
_FILE_CUT_NOTE = "Lines in the middle were not shared. If you need them, ask the user to select them"
# cut_middle's marker: "\n[... <count> characters cut. <note> ...]\n".
_CUT_MARKER_ROOM = len(_FILE_CUT_NOTE) + 40

_PRIME_REQUIRED = "WebNest AI in VS Code is for WebNest Prime members. Get Prime to start chatting."
_PASS_USED = "You have used all the tokens of your Prime pass. Renew it to keep chatting."
_TOO_LARGE = "This conversation is too large. Please start a new chat."
_UNAVAILABLE = "WebNest AI is not available right now. Please try again later."
_DATABASE = "WebNest AI can't reach its records right now. Please try again in a moment."
_NOTHING_TO_ANSWER = "The request has no message to answer"
_WITHHELD_NOTE = {
    "secret": "seems to contain a secret",
    "protected": "is a protected file",
}

Result = TypeVar("Result")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def next_utc_midnight(now: datetime) -> datetime:
    return datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)


def price_display(paise: int) -> str:
    """29900 -> "₹299"; 29950 -> "₹299.50"."""
    rupees, rest = divmod(max(int(paise), 0), 100)
    return f"₹{rupees:,}" if rest == 0 else f"₹{rupees:,}.{rest:02d}"


def start_event(request_id: str) -> bytes:
    """The first event of every VS Code reply; the assistant's own events follow unchanged."""
    data = {"request_id": request_id, "message_id": f"msg_{secrets.token_hex(8)}"}
    return f"event: start\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n".encode("utf-8", "replace")


def _validation(location: list[str | int], message: str) -> ExtensionError:
    return ExtensionError(422, "validation_error", "Request validation failed", errors=[{"loc": location, "msg": message}])


class VSCodeRequestAdapter:
    """Builds the assistant's ChatRequest from a VS Code request. The extension's own limits
    are enforced here, before the assistant's models are built with model_construct: the
    public OpenFile type caps a file at 12,000 characters, and VS Code's files are bigger."""

    def __init__(self, max_context_chars: int) -> None:
        self._max_context_chars = max(int(max_context_chars), 1000)

    def to_chat_request(self, payload: ExtensionChatRequest) -> ChatRequest:
        context = payload.context or ExtContext()
        active = context.active_file
        self._check_segments(active)
        note = self._withheld_note(active)
        messages = self._messages(payload.messages, note)
        chat_context = ChatContext.model_construct(
            language=VSCODE_LANGUAGE,
            files=self._files(context),
            open_file=self._open_file(active),
            last_error=self._problems(context.diagnostics, active.path if active is not None else None),
        )
        try:
            return ChatRequest(mode="ask", messages=messages, context=chat_context)
        except PydanticValidationError as exc:
            # Not expected: everything was checked above. Still a 422, never a 500.
            logger.warning("VS Code request could not be converted: %s errors", exc.error_count())
            raise _validation(["body", "messages"], "The messages could not be read") from exc

    def _check_segments(self, active: ActiveFile | None) -> None:
        if active is None or not active.segments:
            return
        total = sum(len(segment.text) for segment in active.segments)
        if total > self._max_context_chars:
            raise _validation(
                ["body", "context", "active_file", "segments"],
                f"The segments may hold at most {self._max_context_chars} characters in all",
            )

    def _withheld_note(self, active: ActiveFile | None) -> str | None:
        if active is None or not active.withheld:
            return None
        why = _WITHHELD_NOTE.get(active.withheld.lower(), _WITHHELD_NOTE["protected"])
        path = active.path[:_MAX_NOTE_PATH_CHARS]
        return f"(The current file `{path}` was not shared because it {why}. Don't ask the user to paste it.)"

    def _messages(self, received: list[ExtMessage], note: str | None) -> list[ChatMessage]:
        converted = [message for message in (self._message(item) for item in received) if message is not None]
        kept = self._newest_turns(converted)
        last_user = max((index for index, message in enumerate(kept) if message.role == "user"), default=None)
        if last_user is None:
            raise _validation(["body", "messages"], _NOTHING_TO_ANSWER)
        if note is not None:
            message = kept[last_user]
            room = max(MAX_ANY_QUESTION_CHARS - len(note) - 2, 1)
            kept[last_user] = ChatMessage(role="user", content=f"{(message.content or '')[:room]}\n\n{note}")
        return kept

    def _message(self, item: ExtMessage) -> ChatMessage | None:
        """None for a message with nothing in it, which the extension may send."""
        if item.role == "user":
            text = item.content or ""
            if not text.strip():
                return None
            return ChatMessage(role="user", content=text[:MAX_MESSAGE_CHARS])
        if item.role == "assistant":
            calls = [self._tool_call(call) for call in item.tool_calls or []]
            if not (item.content and item.content.strip()) and not calls:
                return None
            return ChatMessage(role="assistant", content=item.content or None, tool_calls=calls or None)
        if not item.tool_call_id:
            return None
        return ChatMessage(
            role="tool",
            tool_call_id=item.tool_call_id,
            content=(item.content or "")[:MAX_TOOL_RESULT_CHARS],
            is_error=bool(item.is_error),
        )

    def _tool_call(self, call: ExtToolCall) -> ChatToolCall:
        arguments: Any = call.arguments
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (ValueError, RecursionError):
                arguments = {}
        return ChatToolCall(id=call.id, name=call.name, arguments=arguments if isinstance(arguments, dict) else {})

    def _newest_turns(self, messages: list[ChatMessage]) -> list[ChatMessage]:
        """At most 60 messages, dropping the oldest whole turns (a turn starts at a user
        message), so a tool call never loses its result."""
        turns: list[list[ChatMessage]] = []
        for message in messages:
            if message.role == "user" or not turns:
                turns.append([])
            turns[-1].append(message)
        total = len(messages)
        while len(turns) > 1 and total > MAX_KEPT_MESSAGES:
            total -= len(turns.pop(0))
        kept = [message for turn in turns for message in turn]
        if len(kept) <= MAX_KEPT_MESSAGES:
            return kept
        # One turn longer than the limit: its user message and its newest messages, starting
        # at an assistant message so no result is left without its call.
        head, tail = kept[0], kept[-(MAX_KEPT_MESSAGES - 1) :]
        while tail and tail[0].role == "tool":
            tail = tail[1:]
        return [head, *tail]

    def _files(self, context: ExtContext) -> list[str]:
        active = context.active_file
        names = ([active.path] if active is not None else []) + list(context.files)
        unique = [name for name in dict.fromkeys(names) if name]
        return unique[:MAX_CONTEXT_FILES]

    def _open_file(self, active: ActiveFile | None) -> OpenFile | None:
        """The shared file text: a header, the selected lines first, then the file segments in
        file order, or the file window. None when nothing of the file was shared."""
        if active is None or active.withheld:
            return None
        pieces: list[str] = []
        selection = active.selection
        if selection is not None:
            if selection.text:
                pieces.append(f"--- selected lines {selection.start_line}-{selection.end_line} ---\n{selection.text}")
            else:
                pieces.append(f"--- selected lines {selection.start_line}-{selection.end_line} (in the file content below) ---")
        if active.segments:
            for segment in sorted(active.segments, key=lambda part: (part.start_line, part.end_line)):
                reason = f" ({segment.reason})" if segment.reason else ""
                pieces.append(f"--- lines {segment.start_line}-{segment.end_line}{reason} ---\n{segment.text}")
        elif active.content:
            pieces.append(f"--- file content from line {active.content_start_line or 1} ---\n{active.content}")
        if not pieces or (selection is not None and not selection.text and len(pieces) == 1):
            return None
        language = f" ({active.language})" if active.language else ""
        text = "\n".join([f"VS Code file: {active.path}{language}", *pieces])
        if len(text) > self._max_context_chars:
            # Room for the cut marker too, so the result fits the optimizer's first tier and
            # is never cut a second time.
            text = cut_middle(text, self._max_context_chars - _CUT_MARKER_ROOM, _FILE_CUT_NOTE)
        return OpenFile.model_construct(name=active.path, content=text)

    def _problems(self, diagnostics: list[Diagnostic], focused_path: str | None) -> str | None:
        """`path:line severity [source]: message`, other files first and the focused file's
        last, because the optimizer keeps the end."""
        if not diagnostics:
            return None
        lines: list[tuple[bool, str]] = []
        for problem in diagnostics:
            message = one_line(problem.message)
            if not message:
                continue
            where = problem.path or "?"
            if problem.line is not None:
                where = f"{where}:{problem.line}"
            severity = f" {problem.severity}" if problem.severity else ""
            source = f" [{problem.source}]" if problem.source else ""
            lines.append((focused_path is not None and problem.path == focused_path, f"{where}{severity}{source}: {message}"))
        if not lines:
            return None
        ordered = [line for focused, line in lines if not focused] + [line for focused, line in lines if focused]
        text = "\n".join(ordered)
        return text if len(text) <= MAX_PROBLEM_CHARS else text[-MAX_PROBLEM_CHARS:]


async def _both(first: Awaitable[Result], second: Awaitable[Any]) -> tuple[Result, Any]:
    """Two independent reads side by side. Both always finish (each has its own time limit and
    hands its connection back) before the first failure, if any, is raised."""
    results = await asyncio.gather(first, second, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return results[0], results[1]


class ExtensionChatService:
    def __init__(
        self,
        settings: Settings,
        assistant: AssistantService,
        payments: AssistantPaymentService,
        usage: AssistantUsageStore,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._settings = settings
        self._assistant = assistant
        self._payments = payments
        self._usage = usage
        self._clock = clock
        self._adapter = VSCodeRequestAdapter(settings.extension_max_context_chars)

    # ---- 2.6 /me --------------------------------------------------------------------------

    async def me(self, session_user: SessionUser) -> MeResponse:
        """Read live on every call, so a payment on the website shows on the next /me."""
        now = self._clock()
        access = await self._access(session_user.user_id, now)
        return MeResponse(
            user=MeUser(id=session_user.user_id, email=session_user.email, full_name=session_user.full_name),
            access=access,
            service=MeService(
                enabled=self._assistant.is_enabled(),
                provider_name=PROVIDER_NAME,
                message=self._settings.extension_service_message.strip() or None,
            ),
            limits=self._limits(),
            links=self._links(),
            extension=MeExtension(
                min_version=self._settings.extension_min_version, latest_version=self._settings.extension_latest_version
            ),
        )

    async def _access(self, user_id: uuid.UUID, now: datetime) -> MeAccess:
        today = now.date()
        if self._payments.paywall_enabled:
            pass_access, totals = await _both(self._payments.get_access(user_id), self._usage.token_totals(user_id, today))
            return self._prime_access(pass_access, totals, now)
        totals, calls = await _both(self._usage.token_totals(user_id, today), self._usage.calls_today(user_id, today))
        return self._free_access(totals, int(calls), now)

    def _prime_access(self, pass_access: AccessResponse, totals: TokenTotals, now: datetime) -> MeAccess:
        """The pass first: once its tokens are gone, waiting for tomorrow won't help."""
        daily_limit = self._settings.assistant_pass_daily_tokens
        weekly_limit = self._settings.assistant_pass_weekly_tokens
        cap: Cap | None = None
        if pass_access.is_prime and pass_access.tokens_used >= pass_access.tokens_limit:
            cap = "pass"
        elif pass_access.is_prime and totals.week >= weekly_limit:
            cap = "week"
        elif pass_access.is_prime and totals.today >= daily_limit:
            cap = "day"
        return MeAccess(
            active=pass_access.active and cap is None,
            is_prime=pass_access.is_prime,
            member_label=pass_access.member_label,
            expires_at=pass_access.expires_at,
            tokens_limit=pass_access.tokens_limit,
            tokens_used=pass_access.tokens_used,
            daily_tokens_limit=daily_limit,
            daily_tokens_used=totals.today,
            weekly_tokens_limit=weekly_limit,
            weekly_tokens_used=totals.week,
            resets_at=next_utc_midnight(now) if cap in ("day", "week") else None,
            price_paise=pass_access.price_paise,
            price_display=price_display(pass_access.price_paise),
            pass_days=pass_access.pass_days,
            cap=cap,
        )

    def _free_access(self, totals: TokenTotals, calls: int, now: datetime) -> MeAccess:
        """Paywall off: every logged-in user has the free daily limits, tokens and calls. There
        is no pass and no weekly limit, so those meters are 0."""
        daily_limit = self._settings.assistant_user_daily_tokens
        used_up = totals.today >= daily_limit or calls >= self._settings.assistant_user_daily_calls
        return MeAccess(
            active=not used_up,
            is_prime=False,
            member_label=None,
            expires_at=None,
            tokens_limit=0,
            tokens_used=0,
            daily_tokens_limit=daily_limit,
            daily_tokens_used=totals.today,
            weekly_tokens_limit=0,
            weekly_tokens_used=totals.week,
            resets_at=next_utc_midnight(now) if used_up else None,
            price_paise=self._settings.assistant_price_paise,
            price_display=price_display(self._settings.assistant_price_paise),
            pass_days=self._settings.assistant_pass_days,
            cap="day" if used_up else None,
        )

    def _limits(self) -> MeLimits:
        return MeLimits(
            max_message_chars=MAX_MESSAGE_CHARS,
            max_input_tokens=self._settings.extension_max_input_tokens,
            max_context_chars=self._settings.extension_max_context_chars,
            chars_per_token=CHARS_PER_TOKEN,
            max_attachments=MAX_ATTACHMENTS,
            max_attachment_bytes=0,
            max_attachments_total_bytes=0,
            max_body_bytes=MAX_BODY_BYTES,
            max_tool_result_chars=MAX_TOOL_RESULT_CHARS,
            max_agent_steps=MAX_AGENT_STEPS,
            allowed_attachment_kinds=[],
            supports_images=False,
        )

    def _links(self) -> MeLinks:
        base = self._settings.frontend_base_url.strip().rstrip("/")
        return MeLinks(
            upgrade_url=f"{base}/prime?source=vscode",
            signup_url=f"{base}/login?next=%2Fprime&source=vscode",
            manage_url=f"{base}/portal?source=vscode",
            terms_url=f"{base}/terms-and-conditions",
            privacy_url=f"{base}/privacy-policy",
            support_url=f"{base}/contact",
        )

    # ---- 2.7 chat -------------------------------------------------------------------------

    def build_request(self, payload: ExtensionChatRequest) -> ChatRequest:
        return self._adapter.to_chat_request(payload)

    async def open_chat(self, user_id: uuid.UUID, payload: ExtensionChatRequest) -> AssistantChatStream:
        """The assistant's own open_chat, whose pass check runs before the provider is
        contacted. On success the caller must eventually close() the stream."""
        request = self._adapter.to_chat_request(payload)
        try:
            return await self._assistant.open_chat(user_id, request)
        except AssistantRateLimitedError as exc:
            if exc.code == "provider_busy":
                raise self._refused(user_id, 503, "provider_busy", exc.message, exc.retry_after or _PROVIDER_BUSY_RETRY_SECONDS) from exc
            raise self._refused(user_id, 429, exc.code, exc.message, exc.retry_after) from exc
        except PaymentRequiredError as exc:
            if await self._is_prime(user_id):
                raise self._refused(user_id, 402, "pass_allowance_used", _PASS_USED) from exc
            raise self._refused(user_id, 402, "prime_required", _PRIME_REQUIRED) from exc
        except PayloadTooLargeError as exc:
            raise self._refused(user_id, 413, "conversation_too_large", _TOO_LARGE) from exc
        except GenerationUnavailableError as exc:
            raise self._refused(user_id, 503, "service_unavailable", _UNAVAILABLE, _UNAVAILABLE_RETRY_SECONDS) from exc
        except DatabaseError as exc:
            raise self._refused(user_id, 503, "service_unavailable", _DATABASE, _DATABASE_RETRY_SECONDS) from exc

    async def _is_prime(self, user_id: uuid.UUID) -> bool:
        """Only to word a 402. If it can't be read, the user is asked to get Prime, which is
        right for everyone but a member whose tokens ran out."""
        try:
            return (await self._payments.get_access(user_id)).is_prime
        except DomainError:
            return False

    def _refused(self, user_id: uuid.UUID, status: int, code: str, message: str, retry_after: int | None = None) -> ExtensionError:
        # One line per refusal, with no request content: refused calls never reach the call
        # log, so allowance pressure is measured from these.
        logger.info("VS Code chat refused: user=%s surface=vscode code=%s status=%d", user_id, code, status)
        return ExtensionError(status, code, message, retry_after)
