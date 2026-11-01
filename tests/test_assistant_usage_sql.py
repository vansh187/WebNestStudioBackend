"""SqlAssistantUsageStore against a real Postgres (the atomic upsert only exists
there). Uses EXAMS_TEST_DATABASE_URL - a throwaway database, never the production
one in .env - and is skipped when it is not set."""

import asyncio
import dataclasses
import os
import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("SUPABASE_URL", "postgresql://user:pass@localhost:5432/test")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from core.exceptions import DatabaseError
from database.assistant_usage_persistence import (
    CallLogEntry,
    DailyLimits,
    PromptSizes,
    SqlAssistantUsageStore,
    TokenCounts,
    UsageKey,
)
from database.models import AssistantCallLog, AssistantUsage, User
from database.session import Base

TEST_DB_URL = os.environ.get("EXAMS_TEST_DATABASE_URL", "")
DAY = date(2026, 10, 31)
TABLES = [User.__table__, AssistantUsage.__table__, AssistantCallLog.__table__]
MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
CALL_LOG_MIGRATIONS = [MIGRATIONS / "015_assistant_call_log.sql", MIGRATIONS / "016_assistant_call_log_turn_chars.sql"]
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


def entry_for(user_id: uuid.UUID, outcome: str = "completed") -> CallLogEntry:
    return CallLogEntry(
        key=key_for(user_id),
        mode="agent",
        outcome=outcome,
        tokens=TokenCounts(100, 20),
        cached_tokens=30,
        exact_usage=True,
        sizes=PromptSizes(
            request_chars=9000,
            sent_chars=800,
            system_chars=600,
            tools_chars=1700,
            file_chars=300,
            context_chars=100,
            user_chars=50,
            tool_result_chars=200,
            history_chars=150,
            turn_assistant_chars=75,
        ),
        first_output_ms=120,
        duration_ms=900,
    )


async def log_rows(db: Db) -> list[AssistantCallLog]:
    async with db.sessions() as session:
        return list((await session.execute(select(AssistantCallLog))).scalars())


def test_a_call_log_row_stores_every_number() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        await db.store.log_call(entry_for(user_id))
        row = (await log_rows(db))[0]
        assert row.id is not None
        assert row.user_id == user_id
        assert (row.provider, row.model, row.mode, row.outcome, row.exact_usage) == ("groq", "m", "agent", "completed", True)
        assert (row.input_tokens, row.output_tokens, row.cached_tokens) == (100, 20, 30)
        assert (row.request_chars, row.sent_chars, row.system_chars, row.tools_chars) == (9000, 800, 600, 1700)
        assert (row.file_chars, row.context_chars, row.user_chars, row.tool_result_chars, row.history_chars) == (300, 100, 50, 200, 150)
        assert row.turn_assistant_chars == 75
        assert (row.first_output_ms, row.duration_ms) == (120, 900)
        assert row.created_at is not None
        assert await db.other_connections() == 0

    scenario(body)


def test_every_call_gets_its_own_row() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        await asyncio.gather(*(db.store.log_call(entry_for(user_id)) for _ in range(10)))
        assert len(await log_rows(db)) == 10
        assert await db.other_connections() == 0

    scenario(body)


def test_a_deleted_accounts_rows_stay_with_no_link_to_the_person() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        await db.store.log_call(entry_for(user_id))
        async with db.engine.begin() as connection:
            await connection.execute(text("delete from users where id = :u"), {"u": user_id})
        rows = await log_rows(db)
        assert [(row.user_id, row.input_tokens) for row in rows] == [(None, 100)]

    scenario(body)


def test_the_purge_removes_only_rows_older_than_the_cutoff() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        await db.store.log_call(entry_for(user_id, "old"))
        await db.store.log_call(entry_for(user_id, "new"))
        async with db.engine.begin() as connection:
            await connection.execute(text("update assistant_call_log set created_at = now() - interval '100 days' where outcome = 'old'"))
        removed = await db.store.purge_call_log(datetime.now(timezone.utc) - timedelta(days=90))
        assert removed == 1
        assert [row.outcome for row in await log_rows(db)] == ["new"]
        assert await db.store.purge_call_log(datetime.now(timezone.utc) - timedelta(days=90)) == 0
        assert await db.other_connections() == 0

    scenario(body)


def test_the_purge_clears_a_backlog_in_batches() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        for _ in range(7):
            await db.store.log_call(entry_for(user_id, "old"))
        await db.store.log_call(entry_for(user_id, "new"))
        async with db.engine.begin() as connection:
            await connection.execute(text("update assistant_call_log set created_at = now() - interval '100 days' where outcome = 'old'"))
        assert await db.store.purge_call_log(datetime.now(timezone.utc) - timedelta(days=90), batch_size=2) == 7
        assert [row.outcome for row in await log_rows(db)] == ["new"]
        assert await db.other_connections() == 0

    scenario(body)


def test_a_failed_call_log_write_is_a_clean_error_and_frees_its_connection() -> None:
    async def body(db: Db) -> None:
        with pytest.raises(DatabaseError):
            # Beyond the integer column's range, so the database refuses the row.
            await db.store.log_call(dataclasses.replace(entry_for(await db.user()), duration_ms=2**40))
        assert await db.other_connections() == 0
        assert await log_rows(db) == []

    scenario(body)


def test_a_stuck_call_log_write_times_out_and_frees_its_connection() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        impatient = SqlAssistantUsageStore(db.sessions, timeout_seconds=0.3)
        async with db.sessions() as blocker:
            await blocker.execute(text("lock table assistant_call_log in access exclusive mode"))
            with pytest.raises(DatabaseError):
                await impatient.log_call(entry_for(user_id))
            with pytest.raises(DatabaseError):
                await impatient.purge_call_log(datetime.now(timezone.utc))
            await blocker.rollback()
        assert await db.other_connections() == 0
        assert await log_rows(db) == []

    scenario(body)


def test_the_migration_builds_the_same_table_as_the_model_and_can_rerun() -> None:
    async def columns(db: Db) -> list[tuple[str, str, str]]:
        async with db.engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "select column_name, data_type, is_nullable from information_schema.columns "
                    "where table_name = 'assistant_call_log' order by column_name"
                )
            )
            return [tuple(row) for row in rows]

    async def indexes(db: Db) -> list[str]:
        async with db.engine.connect() as connection:
            rows = await connection.execute(
                text("select indexname || ': ' || split_part(indexdef, ' USING ', 2) from pg_indexes where tablename = 'assistant_call_log' order by 1")
            )
            return [row[0] for row in rows]

    async def run_migration(db: Db) -> None:
        async with db.engine.connect() as connection:
            autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
            raw = await autocommit.get_raw_connection()
            for migration in CALL_LOG_MIGRATIONS:
                await raw.driver_connection.execute(migration.read_text(encoding="utf-8"))

    async def body(db: Db) -> None:
        from_model = (await columns(db), await indexes(db))
        async with db.engine.begin() as connection:
            await connection.execute(text("drop table assistant_call_log"))
        await run_migration(db)
        await run_migration(db)
        assert (await columns(db), await indexes(db)) == from_model
        # Startup's create_all must leave an existing table exactly as the migrations built it.
        async with db.engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=TABLES))
        assert (await columns(db), await indexes(db)) == from_model
        async with db.engine.connect() as connection:
            secured = await connection.execute(text("select relrowsecurity from pg_class where relname = 'assistant_call_log'"))
            assert secured.scalar_one() is True
        # The owner bypasses row level security, so the app's own writes still work.
        await db.store.log_call(entry_for(await db.user()))
        assert len(await log_rows(db)) == 1

    scenario(body)


def test_an_unreachable_database_is_a_clean_error() -> None:
    async def main() -> None:
        engine = create_async_engine("postgresql+asyncpg://nobody@127.0.0.1:1/none", poolclass=NullPool)
        store = SqlAssistantUsageStore(async_sessionmaker(engine, class_=AsyncSession), timeout_seconds=5)
        with pytest.raises(DatabaseError):
            await store.calls_today(uuid.uuid4(), DAY)
        await engine.dispose()

    asyncio.run(main())
