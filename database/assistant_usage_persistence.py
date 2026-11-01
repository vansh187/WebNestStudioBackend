"""Daily assistant calls and tokens. Each method opens its own short session with a
time limit and always hands the connection back, so none is held while a reply
streams, and a stuck database can't hang a request."""

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Literal, Protocol, cast

from sqlalchemy import ColumnElement, CursorResult, Executable, case, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import DatabaseError
from database.models import AssistantCallLog, AssistantUsage

# Shorter than the pool's own 30-second wait, so a full pool fails fast too.
DB_TIMEOUT_SECONDS = 10.0
PURGE_BATCH_SIZE = 5000
_READ_ERROR = "Could not read assistant usage right now. Please try again shortly."
_WRITE_ERROR = "Could not save assistant usage right now. Please try again shortly."


@dataclass(frozen=True)
class UsageKey:
    """One row of assistant_usage."""

    user_id: uuid.UUID
    day: date
    provider: str
    model: str


@dataclass(frozen=True)
class TokenCounts:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class DailyLimits:
    user_calls: int
    user_tokens: int
    global_tokens: int


@dataclass(frozen=True)
class CallReservation:
    denied: Literal["global_tokens", "user_calls", "user_tokens"] | None
    calls_today: int


@dataclass(frozen=True)
class PromptSizes:
    """Characters only, never the text itself: what arrived, what was sent and where it went."""

    request_chars: int = 0
    sent_chars: int = 0
    system_chars: int = 0
    tools_chars: int = 0
    file_chars: int = 0
    context_chars: int = 0
    user_chars: int = 0
    tool_result_chars: int = 0
    history_chars: int = 0
    turn_assistant_chars: int = 0


@dataclass(frozen=True)
class CallLogEntry:
    """One row of assistant_call_log."""

    key: UsageKey
    mode: str
    outcome: str
    tokens: TokenCounts
    cached_tokens: int
    exact_usage: bool
    sizes: PromptSizes
    first_output_ms: int | None
    duration_ms: int


class AssistantUsageStore(Protocol):
    async def reserve_call(self, key: UsageKey, limits: DailyLimits, reserved: TokenCounts) -> CallReservation: ...

    async def release_call(self, key: UsageKey, reserved: TokenCounts) -> None: ...

    async def settle_tokens(self, key: UsageKey, reserved: TokenCounts, actual: TokenCounts) -> None: ...

    async def calls_today(self, user_id: uuid.UUID, day: date) -> int: ...

    async def log_call(self, entry: CallLogEntry) -> None: ...

    async def purge_call_log(self, before: datetime, batch_size: int = PURGE_BATCH_SIZE) -> int: ...


class SqlAssistantUsageStore:
    def __init__(self, session_factory: Callable[[], AsyncSession], timeout_seconds: float = DB_TIMEOUT_SECONDS) -> None:
        self._session_factory = session_factory
        self._timeout_seconds = timeout_seconds

    @asynccontextmanager
    async def _session(self, error_message: str) -> AsyncIterator[AsyncSession]:
        """A session that is always closed, which rolls back anything not committed and
        returns its connection, however the block ends. OSError: a refused connection
        reaches here unwrapped by SQLAlchemy."""
        try:
            async with asyncio.timeout(self._timeout_seconds):
                async with self._session_factory() as session:
                    yield session
        except (SQLAlchemyError, OSError, TimeoutError) as exc:
            raise DatabaseError(error_message) from exc

    async def reserve_call(self, key: UsageKey, limits: DailyLimits, reserved: TokenCounts) -> CallReservation:
        """Adds 1 to the user's calls and charges the most this call can spend, then decides
        within the same transaction, undoing both when over a limit. The upsert locks the
        user's row, so parallel requests wait for each other here and can't slip past a
        limit. Charging up front means replies still in flight count towards the caps."""
        upsert = insert(AssistantUsage).values(
            user_id=key.user_id,
            day=key.day,
            provider=key.provider,
            model=key.model,
            calls=1,
            input_tokens=reserved.input_tokens,
            output_tokens=reserved.output_tokens,
        )
        upsert = upsert.on_conflict_do_update(
            index_elements=[AssistantUsage.user_id, AssistantUsage.day, AssistantUsage.provider, AssistantUsage.model],
            set_={
                "calls": AssistantUsage.calls + 1,
                "input_tokens": AssistantUsage.input_tokens + reserved.input_tokens,
                "output_tokens": AssistantUsage.output_tokens + reserved.output_tokens,
            },
        )
        row_tokens = AssistantUsage.input_tokens + AssistantUsage.output_tokens
        # One aggregate instead of several queries: this runs before every model call.
        totals = select(
            func.coalesce(func.sum(row_tokens), 0),
            func.coalesce(func.sum(case((AssistantUsage.user_id == key.user_id, AssistantUsage.calls), else_=0)), 0),
            func.coalesce(func.sum(case((AssistantUsage.user_id == key.user_id, row_tokens), else_=0)), 0),
        ).where(AssistantUsage.day == key.day)
        async with self._session(_WRITE_ERROR) as session:
            await session.execute(upsert)
            tokens_today, calls_today, user_tokens_today = (await session.execute(totals)).one()
            denied: Literal["global_tokens", "user_calls", "user_tokens"] | None = None
            if int(tokens_today) >= limits.global_tokens:
                denied = "global_tokens"
            elif int(calls_today) > limits.user_calls:
                denied = "user_calls"
            elif int(user_tokens_today) >= limits.user_tokens:
                denied = "user_tokens"
            if denied is not None:
                await session.rollback()
                return CallReservation(denied=denied, calls_today=int(calls_today) - 1)
            await session.commit()
            return CallReservation(denied=None, calls_today=int(calls_today))

    async def release_call(self, key: UsageKey, reserved: TokenCounts) -> None:
        """Takes a reservation back, for a call the provider refused before any reply."""
        await self._write(
            update(AssistantUsage)
            .where(*self._row(key))
            .values(
                calls=func.greatest(AssistantUsage.calls - 1, 0),
                input_tokens=func.greatest(AssistantUsage.input_tokens - reserved.input_tokens, 0),
                output_tokens=func.greatest(AssistantUsage.output_tokens - reserved.output_tokens, 0),
            )
        )

    async def settle_tokens(self, key: UsageKey, reserved: TokenCounts, actual: TokenCounts) -> None:
        """Replaces a call's reservation with what it really spent."""
        await self._write(
            update(AssistantUsage)
            .where(*self._row(key))
            .values(
                input_tokens=func.greatest(AssistantUsage.input_tokens + actual.input_tokens - reserved.input_tokens, 0),
                output_tokens=func.greatest(AssistantUsage.output_tokens + actual.output_tokens - reserved.output_tokens, 0),
            )
        )

    async def calls_today(self, user_id: uuid.UUID, day: date) -> int:
        statement = select(func.coalesce(func.sum(AssistantUsage.calls), 0)).where(
            AssistantUsage.user_id == user_id, AssistantUsage.day == day
        )
        async with self._session(_READ_ERROR) as session:
            return int((await session.execute(statement)).scalar_one())

    async def log_call(self, entry: CallLogEntry) -> None:
        await self._write(
            insert(AssistantCallLog).values(
                user_id=entry.key.user_id,
                provider=entry.key.provider,
                model=entry.key.model,
                mode=entry.mode,
                outcome=entry.outcome,
                input_tokens=entry.tokens.input_tokens,
                output_tokens=entry.tokens.output_tokens,
                cached_tokens=entry.cached_tokens,
                exact_usage=entry.exact_usage,
                first_output_ms=entry.first_output_ms,
                duration_ms=entry.duration_ms,
                **asdict(entry.sizes),
            )
        )

    async def purge_call_log(self, before: datetime, batch_size: int = PURGE_BATCH_SIZE) -> int:
        """Deletes rows older than `before`, and returns how many went. In batches, each
        with its own time limit, so a large backlog still shrinks if a later batch fails."""
        doomed = select(AssistantCallLog.id).where(AssistantCallLog.created_at < before).limit(batch_size)
        statement = delete(AssistantCallLog).where(AssistantCallLog.id.in_(doomed)).execution_options(synchronize_session=False)
        removed = 0
        while True:
            async with self._session(_WRITE_ERROR) as session:
                result = await session.execute(statement)
                await session.commit()
                batch = cast(CursorResult[Any], result).rowcount
            removed += batch
            if batch < batch_size:
                return removed

    def _row(self, key: UsageKey) -> tuple[ColumnElement[bool], ...]:
        return (
            AssistantUsage.user_id == key.user_id,
            AssistantUsage.day == key.day,
            AssistantUsage.provider == key.provider,
            AssistantUsage.model == key.model,
        )

    async def _write(self, statement: Executable) -> None:
        async with self._session(_WRITE_ERROR) as session:
            await session.execute(statement)
            await session.commit()
