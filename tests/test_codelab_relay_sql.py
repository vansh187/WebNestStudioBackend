"""SqlDailyCounter against a real Postgres (the atomic upserts only exist
there). Uses EXAMS_TEST_DATABASE_URL - a throwaway database, never the
production one in .env - and is skipped when it is not set."""

import asyncio
import os
import uuid
from datetime import date, datetime, timedelta, timezone

os.environ.setdefault("SUPABASE_URL", "postgresql://user:pass@localhost:5432/test")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from codelab_relay import errors
from codelab_relay.infra.sql_daily_counter import SqlDailyCounter
from codelab_relay.models import RelayDailyUsage, RelayGlobalDailyUsage
from database.models import User
from database.session import Base

TEST_DB_URL = os.environ.get("EXAMS_TEST_DATABASE_URL", "")
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
TABLES = [User.__table__, RelayDailyUsage.__table__, RelayGlobalDailyUsage.__table__]

pytestmark = pytest.mark.skipif(not TEST_DB_URL, reason="EXAMS_TEST_DATABASE_URL is not set")


def async_url(url: str) -> str:
    return url.replace("postgresql://", "postgresql+asyncpg://", 1) if url.startswith("postgresql://") else url


class Db:
    def __init__(self) -> None:
        self.engine = create_async_engine(async_url(TEST_DB_URL), poolclass=NullPool)
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

    async def reset(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.drop_all(sync, tables=TABLES[1:]))
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=TABLES))
            await connection.execute(text("delete from users where email like 'relay-%@test.local'"))

    async def user(self) -> uuid.UUID:
        user_id = uuid.uuid4()
        async with self.sessions() as session:
            session.add(User(id=user_id, email=f"relay-{user_id}@test.local", password_hash="x"))
            await session.commit()
        return user_id

    def counter(self, per_day=3, global_per_day=100) -> SqlDailyCounter:
        return SqlDailyCounter(self.sessions, per_day, global_per_day)

    async def take(self, user_id, now=NOW, per_day=3, global_per_day=100) -> int:
        return await self.counter(per_day, global_per_day).take(user_id, now)

    async def remaining(self, user_id, now=NOW, per_day=3) -> int:
        return await self.counter(per_day).remaining(user_id, now)

    async def counts(self) -> tuple[dict, dict]:
        async with self.sessions() as session:
            users = {(row.user_id, row.day): row.count for row in (await session.scalars(select(RelayDailyUsage)))}
            everyone = {row.day: row.count for row in (await session.scalars(select(RelayGlobalDailyUsage)))}
            return users, everyone


def scenario(body):
    async def main():
        db = Db()
        try:
            await db.reset()
            await body(db)
        finally:
            await db.engine.dispose()

    asyncio.run(main())


def test_user_quota_counts_and_refuses_without_overcounting():
    async def body(db):
        user = await db.user()
        assert [await db.take(user) for _ in range(3)] == [2, 1, 0]
        for _ in range(2):
            with pytest.raises(errors.RateLimited) as caught:
                await db.take(user)
            assert caught.value.extra == {"retry_after": 12 * 3600, "scope": "day"}
        users, everyone = await db.counts()
        assert users == {(user, date(2026, 10, 24)): 3} and everyone == {date(2026, 10, 24): 3}
        assert await db.remaining(user) == 0
        assert await db.take(user, now=NOW + timedelta(days=1)) == 2   # a new UTC day

    scenario(body)


def test_concurrent_calls_never_exceed_the_quota():
    async def body(db):
        user = await db.user()

        async def attempt():
            try:
                await db.take(user, per_day=5)
                return True
            except errors.RateLimited:
                return False

        results = await asyncio.gather(*(attempt() for _ in range(20)))
        assert results.count(True) == 5
        users, _ = await db.counts()
        assert users[(user, date(2026, 10, 24))] == 5

    scenario(body)


def test_global_cap_refuses_and_refunds_the_user():
    async def body(db):
        first, second = await db.user(), await db.user()
        await db.take(first, global_per_day=2)
        await db.take(second, global_per_day=2)
        with pytest.raises(errors.RateLimited) as caught:
            await db.take(second, global_per_day=2)
        assert caught.value.extra["scope"] == "global"
        users, everyone = await db.counts()
        assert users[(second, date(2026, 10, 24))] == 1 and everyone[date(2026, 10, 24)] == 2

    scenario(body)


def test_give_back_undoes_one_call_and_never_goes_below_zero():
    async def body(db):
        user = await db.user()
        await db.take(user)
        await db.take(user)
        await db.counter().give_back(user, NOW)
        users, everyone = await db.counts()
        assert users[(user, date(2026, 10, 24))] == 1 and everyone[date(2026, 10, 24)] == 1
        for _ in range(3):
            await db.counter().give_back(user, NOW)
        await db.counter().give_back(await db.user(), NOW)   # a user with no row yet
        users, everyone = await db.counts()
        assert users[(user, date(2026, 10, 24))] == 0 and everyone[date(2026, 10, 24)] == 0

    scenario(body)


def test_database_failures_become_relay_disabled_and_leave_nothing_behind():
    async def body(db):
        user = await db.user()
        broken = create_async_engine("postgresql+asyncpg://nobody@127.0.0.1:1/none", poolclass=NullPool)
        try:
            counter = SqlDailyCounter(async_sessionmaker(broken, class_=AsyncSession), 3, 100)
            for call in (counter.take(user, NOW), counter.give_back(user, NOW), counter.remaining(user, NOW),
                         counter.purge_before(date(2026, 1, 1))):
                with pytest.raises(errors.RelayDisabled):
                    await call
        finally:
            await broken.dispose()
        # An upsert that fails mid-transaction (a user that doesn't exist breaks
        # the foreign key) is rolled back and reported the same way.
        with pytest.raises(errors.RelayDisabled):
            await db.take(uuid.uuid4())
        assert await db.counts() == ({}, {})

    scenario(body)


def test_purge_and_cascade_on_user_delete():
    async def body(db):
        user = await db.user()
        await db.take(user, now=NOW - timedelta(days=40))
        await db.take(user)
        assert await db.counter().purge_before(date(2026, 9, 24)) == 2
        users, everyone = await db.counts()
        assert list(users) == [(user, date(2026, 10, 24))] and list(everyone) == [date(2026, 10, 24)]
        async with db.sessions() as session:
            await session.execute(text("delete from users where id = :id"), {"id": user})
            await session.commit()
        assert (await db.counts())[0] == {}

    scenario(body)
