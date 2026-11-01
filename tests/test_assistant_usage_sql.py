"""SqlAssistantUsageStore against a real Postgres (the atomic upsert only exists
there). Uses EXAMS_TEST_DATABASE_URL - a throwaway database, never the production
one in .env - and is skipped when it is not set."""

import asyncio
import os
import uuid
from collections.abc import Awaitable, Callable
from datetime import date

os.environ.setdefault("SUPABASE_URL", "postgresql://user:pass@localhost:5432/test")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from core.exceptions import DatabaseError
from database.assistant_usage_persistence import DailyLimits, SqlAssistantUsageStore, TokenCounts, UsageKey
from database.models import AssistantUsage, User
from database.session import Base

TEST_DB_URL = os.environ.get("EXAMS_TEST_DATABASE_URL", "")
DAY = date(2026, 10, 31)
TABLES = [User.__table__, AssistantUsage.__table__]
NO_TOKENS = TokenCounts()

pytestmark = pytest.mark.skipif(not TEST_DB_URL, reason="EXAMS_TEST_DATABASE_URL is not set")


def async_url(url: str) -> str:
    return url.replace("postgresql://", "postgresql+asyncpg://", 1) if url.startswith("postgresql://") else url


def limits(user_calls: int = 5, user_tokens: int = 100_000, global_tokens: int = 1000) -> DailyLimits:
    return DailyLimits(user_calls=user_calls, user_tokens=user_tokens, global_tokens=global_tokens)


def key_for(user_id: uuid.UUID, model: str = "m", day: date = DAY) -> UsageKey:
    return UsageKey(user_id=user_id, day=day, provider="groq", model=model)


class Db:
    def __init__(self) -> None:
        self.engine = create_async_engine(async_url(TEST_DB_URL), poolclass=NullPool)
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        self.store = SqlAssistantUsageStore(self.sessions)

    async def reset(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.drop_all(sync, tables=TABLES[1:]))
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=TABLES))
            await connection.execute(text("delete from users where email like 'assistant-%@test.local'"))

    async def user(self) -> uuid.UUID:
        user_id = uuid.uuid4()
        async with self.sessions() as session:
            session.add(User(id=user_id, email=f"assistant-{user_id}@test.local", password_hash="x"))
            await session.commit()
        return user_id

    async def row(self, user_id: uuid.UUID, model: str = "m") -> AssistantUsage | None:
        async with self.sessions() as session:
            return await session.scalar(select(AssistantUsage).where(AssistantUsage.user_id == user_id, AssistantUsage.model == model))

    async def other_connections(self) -> int:
        """Connections to this database besides the one asking. With NullPool, any other
        is one that was never handed back. Waits briefly: closing one takes a moment."""
        count = -1
        for _ in range(40):
            async with self.engine.connect() as connection:
                result = await connection.execute(
                    text("select count(*) from pg_stat_activity where datname = current_database() and pid <> pg_backend_pid()")
                )
                count = int(result.scalar_one())
            if count == 0:
                return 0
            await asyncio.sleep(0.05)
        return count


def scenario(body: Callable[[Db], Awaitable[None]]) -> None:
    async def main() -> None:
        db = Db()
        try:
            await db.reset()
            await body(db)
        finally:
            await db.engine.dispose()

    asyncio.run(main())


def test_parallel_reservations_can_not_pass_the_limit() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        results = await asyncio.gather(*(db.store.reserve_call(key_for(user_id), limits(), NO_TOKENS) for _ in range(20)))
        allowed = [result for result in results if result.denied is None]
        assert len(allowed) == 5
        assert sorted(result.calls_today for result in allowed) == [1, 2, 3, 4, 5]
        assert all(result.denied == "user_calls" for result in results if result.denied is not None)
        row = await db.row(user_id)
        assert row is not None and row.calls == 5
        assert await db.other_connections() == 0

    scenario(body)


def test_the_daily_limit_covers_every_model() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        assert (await db.store.reserve_call(key_for(user_id, "a"), limits(user_calls=2), NO_TOKENS)).calls_today == 1
        assert (await db.store.reserve_call(key_for(user_id, "b"), limits(user_calls=2), NO_TOKENS)).calls_today == 2
        assert (await db.store.reserve_call(key_for(user_id, "c"), limits(user_calls=2), NO_TOKENS)).denied == "user_calls"
        assert await db.store.calls_today(user_id, DAY) == 2

    scenario(body)


def test_the_global_cap_counts_every_user_and_rolls_the_call_back() -> None:
    async def body(db: Db) -> None:
        first, second = await db.user(), await db.user()
        await db.store.reserve_call(key_for(first), limits(global_tokens=100), NO_TOKENS)
        await db.store.settle_tokens(key_for(first), NO_TOKENS, TokenCounts(60, 40))
        assert (await db.store.reserve_call(key_for(second), limits(global_tokens=100), NO_TOKENS)).denied == "global_tokens"
        assert await db.store.calls_today(second, DAY) == 0

    scenario(body)


def test_replies_still_in_flight_count_towards_the_global_cap() -> None:
    async def body(db: Db) -> None:
        first, second = await db.user(), await db.user()
        reserved = TokenCounts(500, 200)
        assert (await db.store.reserve_call(key_for(first), limits(global_tokens=1000), reserved)).denied is None
        # Nothing has finished yet, and a second reservation still wouldn't fit.
        assert (await db.store.reserve_call(key_for(second), limits(global_tokens=1000), reserved)).denied == "global_tokens"
        second_row = await db.row(second)
        assert second_row is None, "a denied reservation leaves nothing behind"

    scenario(body)


def test_one_user_can_not_use_up_the_whole_budget() -> None:
    async def body(db: Db) -> None:
        heavy, other = await db.user(), await db.user()
        reserved = TokenCounts(70, 30)
        roomy = limits(user_calls=100, user_tokens=250, global_tokens=10_000)
        results = [await db.store.reserve_call(key_for(heavy), roomy, reserved) for _ in range(3)]
        assert [result.denied for result in results] == [None, None, "user_tokens"]
        assert (await db.store.reserve_call(key_for(other), roomy, reserved)).denied is None

    scenario(body)


def test_settling_replaces_the_reservation_with_what_was_spent() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        reserved = TokenCounts(500, 200)
        await db.store.reserve_call(key_for(user_id), limits(global_tokens=10_000), reserved)
        await db.store.settle_tokens(key_for(user_id), reserved, TokenCounts(120, 30))
        row = await db.row(user_id)
        assert row is not None and (row.calls, row.input_tokens, row.output_tokens) == (1, 120, 30)

    scenario(body)


def test_a_released_call_gives_back_the_call_and_its_tokens() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        reserved = TokenCounts(500, 200)
        await db.store.reserve_call(key_for(user_id), limits(global_tokens=10_000), NO_TOKENS)
        await db.store.reserve_call(key_for(user_id), limits(global_tokens=10_000), reserved)
        await db.store.release_call(key_for(user_id), reserved)
        row = await db.row(user_id)
        assert row is not None and (row.calls, row.input_tokens, row.output_tokens) == (1, 0, 0)

    scenario(body)


def test_days_are_counted_separately_and_numbers_never_go_negative() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        await db.store.reserve_call(key_for(user_id), limits(user_calls=1), NO_TOKENS)
        await db.store.release_call(key_for(user_id), TokenCounts(5, 5))
        await db.store.release_call(key_for(user_id), TokenCounts(5, 5))
        row = await db.row(user_id)
        assert row is not None and (row.calls, row.input_tokens, row.output_tokens) == (0, 0, 0)
        tomorrow = key_for(user_id, day=date(2026, 11, 1))
        assert (await db.store.reserve_call(tomorrow, limits(user_calls=1), NO_TOKENS)).calls_today == 1

    scenario(body)


def test_no_connection_is_left_open_after_any_outcome() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        await db.store.reserve_call(key_for(user_id), limits(user_calls=1), NO_TOKENS)  # committed
        await db.store.reserve_call(key_for(user_id), limits(user_calls=1), NO_TOKENS)  # denied, rolled back
        await db.store.settle_tokens(key_for(user_id), NO_TOKENS, TokenCounts(1, 1))
        await db.store.release_call(key_for(user_id), NO_TOKENS)
        await db.store.calls_today(user_id, DAY)
        assert await db.other_connections() == 0

    scenario(body)


def test_a_stuck_database_times_out_and_hands_its_connection_back() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        await db.store.reserve_call(key_for(user_id), limits(), NO_TOKENS)
        impatient = SqlAssistantUsageStore(db.sessions, timeout_seconds=0.3)
        async with db.sessions() as blocker:
            # Holds the row's lock, so every write to it has to wait.
            await blocker.execute(update(AssistantUsage).where(AssistantUsage.user_id == user_id).values(calls=9))
            with pytest.raises(DatabaseError):
                await impatient.settle_tokens(key_for(user_id), NO_TOKENS, TokenCounts(1, 1))
            with pytest.raises(DatabaseError):
                await impatient.reserve_call(key_for(user_id), limits(), NO_TOKENS)
            await blocker.rollback()
        assert await db.other_connections() == 0
        row = await db.row(user_id)
        assert row is not None and (row.calls, row.input_tokens) == (1, 0), "the timed-out writes left nothing behind"

    scenario(body)


def test_an_unreachable_database_is_a_clean_error() -> None:
    async def main() -> None:
        engine = create_async_engine("postgresql+asyncpg://nobody@127.0.0.1:1/none", poolclass=NullPool)
        store = SqlAssistantUsageStore(async_sessionmaker(engine, class_=AsyncSession), timeout_seconds=5)
        with pytest.raises(DatabaseError):
            await store.calls_today(uuid.uuid4(), DAY)
        await engine.dispose()

    asyncio.run(main())
