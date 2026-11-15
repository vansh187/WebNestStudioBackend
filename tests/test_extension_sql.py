"""WebNest AI for VS Code against a real Postgres: migrations 024 and 025 themselves, the
atomic single-use claims under parallel requests, the refresh rules, revocation inside the
website's own transaction, the call log's surface and connection hygiene. Uses
EXAMS_TEST_DATABASE_URL - a throwaway database, never the production one in .env - and is
skipped when it is not set. It needs a superuser, to create the roles Supabase provides.

The static checks at the top (the migration matches the models) always run."""

import asyncio
import os
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("SUPABASE_URL", "postgresql://user:pass@localhost:5432/test")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

import asyncpg
import pytest
from sqlalchemy import select, text
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
from database.extension_session_persistence import (
    ExtensionSessionPersistence,
    NewAuthCode,
    NewSession,
    SqlExtensionSessionStore,
)
from database.models import AssistantUsage, ExtensionAuthCode, ExtensionRefreshToken, ExtensionSession, User
from database.session import Base

TEST_DB_URL = os.environ.get("EXAMS_TEST_DATABASE_URL", "")
MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
SESSIONS_SQL = (MIGRATIONS / "024_extension_sessions.sql").read_text(encoding="utf-8")
SURFACE_SQL = (MIGRATIONS / "025_assistant_call_log_surface.sql").read_text(encoding="utf-8")
CALL_LOG_SQL = [(MIGRATIONS / name).read_text(encoding="utf-8") for name in ("015_assistant_call_log.sql", "016_assistant_call_log_turn_chars.sql")]
EXTENSION_TABLES = ("extension_refresh_tokens", "extension_auth_codes", "extension_sessions")
NOW = datetime(2026, 11, 15, 9, 30, tzinfo=timezone.utc)
GRACE = timedelta(seconds=30)
LIFETIME = timedelta(days=30)

needs_db = pytest.mark.skipif(not TEST_DB_URL, reason="EXAMS_TEST_DATABASE_URL is not set")


def surface_parts() -> tuple[str, str]:
    """025 in the two pieces it must run as: the transaction, then the concurrent index alone."""
    start = SURFACE_SQL.index("CREATE INDEX CONCURRENTLY")
    return SURFACE_SQL[:start], SURFACE_SQL[start:]


# ---- the migration matches the models (no database needed) ------------------------------------


@pytest.mark.parametrize("model", [ExtensionSession, ExtensionAuthCode, ExtensionRefreshToken])
def test_024_has_every_column_and_index_of_the_models(model: type) -> None:
    table = model.__table__  # type: ignore[attr-defined]
    start = SESSIONS_SQL.index(f"CREATE TABLE IF NOT EXISTS {table.name} (")
    body = SESSIONS_SQL[start : SESSIONS_SQL.index("\n);", start)]
    in_file = set(re.findall(r"^\s{4}([a-z_]+) ", body, re.MULTILINE)) - {"CONSTRAINT"}
    assert in_file == {column.name for column in table.columns}
    for index in table.indexes:
        assert f"CREATE INDEX IF NOT EXISTS {index.name} ON {table.name}" in SESSIONS_SQL
    assert f"ALTER TABLE {table.name} ENABLE ROW LEVEL SECURITY;" in SESSIONS_SQL
    assert f"REVOKE ALL ON {table.name} FROM anon, authenticated;" in SESSIONS_SQL


def test_025_runs_its_index_outside_the_transaction() -> None:
    transaction, index = surface_parts()
    assert "COMMIT;" in transaction and "BEGIN" not in index
    assert "lock_timeout = '3s'" in transaction and "lock_timeout = '3s'" in SESSIONS_SQL


# ---- the database -------------------------------------------------------------------------------


def async_url(url: str) -> str:
    return url.replace("postgresql://", "postgresql+asyncpg://", 1) if url.startswith("postgresql://") else url


CREATE_ROLES = """
DO $$
DECLARE role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
            EXECUTE format('CREATE ROLE %I NOSUPERUSER NOBYPASSRLS', role_name);
        END IF;
    END LOOP;
END $$;
GRANT USAGE ON SCHEMA public TO anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO anon, authenticated;
"""


class Db:
    def __init__(self) -> None:
        self.engine = create_async_engine(async_url(TEST_DB_URL), poolclass=NullPool)
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        self.store = SqlExtensionSessionStore(self.sessions)
        self.usage = SqlAssistantUsageStore(self.sessions)

    async def raw(self, sql: str) -> None:
        connection = await asyncpg.connect(TEST_DB_URL)
        try:
            await connection.execute(sql)
        finally:
            await connection.close()

    async def reset(self, with_surface: bool = True) -> None:
        """Users and usage from the models; the call log and the extension tables from their
        migrations, exactly as Supabase gets them."""
        await self.raw("DROP TABLE IF EXISTS " + ", ".join(EXTENSION_TABLES) + ", assistant_call_log CASCADE")
        async with self.engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.drop_all(sync, tables=[AssistantUsage.__table__]))
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[User.__table__, AssistantUsage.__table__]))
            await connection.execute(text("delete from users where email like 'ext-%@test.local'"))
        await self.raw(CREATE_ROLES)
        for sql in CALL_LOG_SQL:
            await self.raw(sql)
        await self.raw(SESSIONS_SQL)
        if with_surface:
            for part in surface_parts():
                await self.raw(part)

    async def user(self, active: bool = True, role: str = "client") -> uuid.UUID:
        user_id = uuid.uuid4()
        async with self.sessions() as session:
            session.add(User(id=user_id, email=f"ext-{user_id}@test.local", password_hash="x", is_active=active, role=role))
            await session.commit()
        return user_id

    async def set_active(self, user_id: uuid.UUID, active: bool) -> None:
        async with self.sessions() as session:
            await session.execute(text("update users set is_active = :a where id = :id"), {"a": active, "id": user_id})
            await session.commit()

    async def scalar(self, sql: str, **params: object) -> object:
        async with self.sessions() as session:
            return (await session.execute(text(sql), params)).scalar()

    async def other_connections(self) -> int:
        """Connections to this database besides the one asking: with NullPool any other is one
        that was never handed back. Waits briefly, since closing one takes a moment."""
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

    async def session_for(self, user_id: uuid.UUID, token_hash: str = "t1", expires_in: timedelta = timedelta(days=90)) -> uuid.UUID:
        opened = await self.store.open_session(
            NewSession(user_id, "LAPTOP", "Visual Studio Code", "0.1.0", NOW + expires_in, token_hash, min(NOW + expires_in, NOW + LIFETIME)),
            NOW,
        )
        assert opened.status == "ok" and opened.session_id is not None
        return opened.session_id


def scenario(body: Callable[[Db], Awaitable[None]], with_surface: bool = True) -> None:
    async def main() -> None:
        db = Db()
        try:
            await db.reset(with_surface)
            await body(db)
            assert await db.other_connections() == 0
        finally:
            await db.engine.dispose()

    asyncio.run(main())


def code(user_id: uuid.UUID, code_hash: str = "c1", expires_at: datetime = NOW + timedelta(minutes=5)) -> NewAuthCode:
    return NewAuthCode(code_hash, user_id, "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM", "vscode://webneststudio.webnest-ai/auth", "LAPTOP", "Cursor", "0.1.0", expires_at)


@needs_db
def test_the_migrations_apply_twice_and_lock_out_anon_and_authenticated() -> None:
    async def body(db: Db) -> None:
        await db.raw(SESSIONS_SQL)
        for part in surface_parts():
            await db.raw(part)
        connection = await asyncpg.connect(TEST_DB_URL)
        try:
            for table in (*EXTENSION_TABLES, "assistant_call_log"):
                assert await connection.fetchval("SELECT relrowsecurity FROM pg_class WHERE relname = $1", table)
            for table in EXTENSION_TABLES:
                for role in ("anon", "authenticated"):
                    assert not await connection.fetchval("SELECT has_table_privilege($1, $2, 'SELECT')", role, table)
                    assert not await connection.fetchval("SELECT has_table_privilege($1, $2, 'INSERT')", role, table)
            index = await connection.fetchrow(
                "SELECT indisvalid FROM pg_index JOIN pg_class ON pg_class.oid = indexrelid WHERE relname = 'ix_assistant_call_log_surface_created'"
            )
            assert index is not None and index["indisvalid"]
            assert await connection.fetchval(
                "SELECT column_default FROM information_schema.columns WHERE table_name = 'assistant_call_log' AND column_name = 'surface'"
            ) == "'codelab'::text"
            with pytest.raises(asyncpg.CheckViolationError):
                await connection.execute(
                    "INSERT INTO extension_sessions (user_id, expires_at, revoked_reason) VALUES ($1, now(), 'stolen')", await db.user()
                )
        finally:
            await connection.close()

    scenario(body)


@needs_db
def test_twenty_parallel_exchanges_of_one_code_let_exactly_one_through() -> None:
    async def body(db: Db) -> None:
        user = await db.user()
        await db.store.create_code(code(user))
        results = await asyncio.gather(*(db.store.consume_code("c1", NOW) for _ in range(20)))
        won = [result for result in results if result is not None]
        assert len(won) == 1 and won[0].user_id == user and won[0].app_name == "Cursor"
        assert await db.store.consume_code("c1", NOW) is None

    scenario(body)


@needs_db
def test_an_expired_or_unknown_code_is_never_consumed() -> None:
    async def body(db: Db) -> None:
        user = await db.user()
        await db.store.create_code(code(user, "old", NOW))
        assert await db.store.consume_code("old", NOW) is None
        assert await db.store.consume_code("nothing", NOW) is None
        assert await db.scalar("select used_at from extension_auth_codes where code_hash = 'old'") is None

    scenario(body)


@needs_db
def test_a_session_opens_only_for_an_active_account() -> None:
    async def body(db: Db) -> None:
        active, disabled = await db.user(role="admin"), await db.user(active=False)
        opened = await db.store.open_session(NewSession(active, "D", "A", "0.1.0", NOW + timedelta(days=90), "t-a", NOW + LIFETIME), NOW)
        assert opened.status == "ok" and opened.role == "admin"
        refused = await db.store.open_session(NewSession(disabled, "D", "A", "0.1.0", NOW + timedelta(days=90), "t-d", NOW + LIFETIME), NOW)
        assert refused.status == "disabled"
        missing = await db.store.open_session(NewSession(uuid.uuid4(), "D", "A", "0.1.0", NOW + timedelta(days=90), "t-m", NOW + LIFETIME), NOW)
        assert missing.status == "missing"
        assert await db.scalar("select count(*) from extension_sessions") == 1
        assert await db.scalar("select count(*) from extension_refresh_tokens") == 1

    scenario(body)


@needs_db
def test_refresh_rotates_keeps_the_end_and_records_the_version() -> None:
    async def body(db: Db) -> None:
        user = await db.user()
        session_id = await db.session_for(user)
        later = NOW + timedelta(days=80)
        result = await db.store.refresh("t1", "t2", NOW + timedelta(days=1), GRACE, LIFETIME, "0.2.0")
        assert result.status == "ok" and result.session_id == session_id and result.user_id == user
        assert result.token_expires_at == NOW + timedelta(days=31)
        assert (await db.store.refresh("t2", "t3", NOW + timedelta(days=30), GRACE, LIFETIME, None)).status == "ok"
        assert (await db.store.refresh("t3", "t4", NOW + timedelta(days=59), GRACE, LIFETIME, None)).status == "ok"
        near_end = await db.store.refresh("t4", "t5", later, GRACE, LIFETIME, None)
        # Capped at the session's end, which never moved.
        assert near_end.token_expires_at == NOW + timedelta(days=90)
        assert (await db.store.refresh("t1", "x", NOW + timedelta(days=2), GRACE, LIFETIME, None)).status == "reused"
        async with db.sessions() as session:
            row = await session.get(ExtensionSession, session_id)
        assert row is not None and row.expires_at == NOW + timedelta(days=90) and row.ext_version == "0.2.0" and row.last_used_at == later

    scenario(body)


@needs_db
def test_parallel_refreshes_of_one_token_all_get_a_pair_inside_the_grace() -> None:
    async def body(db: Db) -> None:
        user = await db.user()
        session_id = await db.session_for(user)
        results = await asyncio.gather(*(db.store.refresh("t1", f"new-{n}", NOW, GRACE, LIFETIME, None) for n in range(10)))
        assert {result.status for result in results} == {"ok"} and {result.session_id for result in results} == {session_id}
        assert await db.scalar("select count(*) from extension_refresh_tokens where used_at is null") == 10

    scenario(body)


@needs_db
def test_reuse_after_the_grace_revokes_the_session_and_the_newest_token_dies() -> None:
    async def body(db: Db) -> None:
        user = await db.user()
        session_id = await db.session_for(user)
        assert (await db.store.refresh("t1", "t2", NOW, GRACE, LIFETIME, None)).status == "ok"
        assert (await db.store.refresh("t1", "t3", NOW + timedelta(seconds=30), GRACE, LIFETIME, None)).status == "ok"
        reused = await db.store.refresh("t1", "t4", NOW + timedelta(seconds=31), GRACE, LIFETIME, None)
        assert reused.status == "reused" and reused.session_id == session_id
        assert await db.scalar("select revoked_reason from extension_sessions where id = :id", id=session_id) == "reuse"
        assert (await db.store.refresh("t2", "t5", NOW + timedelta(seconds=32), GRACE, LIFETIME, None)).status == "revoked"

    scenario(body)


@needs_db
def test_refresh_refuses_disabled_revoked_expired_and_unknown() -> None:
    async def body(db: Db) -> None:
        user = await db.user()
        await db.session_for(user, "a")
        await db.set_active(user, False)
        assert (await db.store.refresh("a", "a2", NOW, GRACE, LIFETIME, None)).status == "disabled"
        await db.set_active(user, True)

        revoked = await db.session_for(user, "b")
        assert await db.store.revoke_session(user, revoked, NOW, "user")
        assert (await db.store.refresh("b", "b2", NOW, GRACE, LIFETIME, None)).status == "revoked"

        await db.session_for(user, "c", expires_in=timedelta(days=90))
        assert (await db.store.refresh("c", "c2", NOW + timedelta(days=30), GRACE, LIFETIME, None)).status == "expired"

        await db.session_for(user, "d", expires_in=timedelta(days=1))
        assert (await db.store.refresh("d", "d2", NOW + timedelta(days=1), GRACE, LIFETIME, None)).status == "expired"

        assert (await db.store.refresh("nothing", "x", NOW, GRACE, LIFETIME, None)).status == "unknown"

    scenario(body)


@needs_db
def test_session_user_revoke_list_and_cascade() -> None:
    async def body(db: Db) -> None:
        user, other = await db.user(), await db.user()
        first = await db.session_for(user, "u1")
        second = await db.session_for(user, "u2")
        await db.session_for(other, "o1")

        seen = await db.store.session_user(first)
        assert seen is not None and seen.user_id == user and seen.user_active and not seen.revoked
        assert await db.store.session_user(uuid.uuid4()) is None

        assert not await db.store.revoke_session(other, first, NOW, "user")
        assert await db.store.revoke_by_token("u2", NOW, "logout")
        assert not await db.store.revoke_by_token("u2", NOW, "logout")
        listed = await db.store.list_sessions(user, NOW)
        assert [summary.session_id for summary in listed] == [first]
        assert listed[0].app_name == "Visual Studio Code"
        assert await db.store.revoke_all_for_user(user, NOW, "password_reset") == 1
        assert await db.store.list_sessions(user, NOW) == []
        assert await db.scalar("select revoked_reason from extension_sessions where id = :id", id=second) == "logout"

        await db.store.create_code(code(other, "oc"))
        async with db.sessions() as session:
            await session.execute(text("delete from users where id = :id"), {"id": other})
            await session.commit()
        assert await db.scalar("select count(*) from extension_sessions where user_id = :id", id=other) == 0
        assert await db.scalar("select count(*) from extension_refresh_tokens where token_hash = 'o1'") == 0
        assert await db.scalar("select count(*) from extension_auth_codes where code_hash = 'oc'") == 0

    scenario(body)


@needs_db
def test_the_purge_removes_only_what_ended_long_enough_ago() -> None:
    async def body(db: Db) -> None:
        user = await db.user()
        live = await db.session_for(user, "live")
        ended = await db.session_for(user, "ended")
        await db.store.revoke_session(user, ended, NOW, "user")
        await db.store.create_code(code(user, "fresh", NOW + timedelta(minutes=5)))
        await db.store.create_code(code(user, "stale", NOW - timedelta(days=2)))
        removed = await db.store.purge(NOW - timedelta(days=1), NOW - timedelta(days=30))
        assert removed == 1
        assert await db.scalar("select count(*) from extension_auth_codes") == 1
        removed = await db.store.purge(NOW + timedelta(days=31), NOW + timedelta(days=1))
        assert await db.scalar("select count(*) from extension_sessions where id = :id", id=ended) == 0
        assert await db.scalar("select count(*) from extension_sessions where id = :id", id=live) == 1

    scenario(body)


@needs_db
def test_ending_sessions_inside_the_websites_transaction_commits_or_rolls_back_with_it() -> None:
    async def body(db: Db) -> None:
        user = await db.user()
        session_id = await db.session_for(user)
        async with db.sessions() as session:
            await ExtensionSessionPersistence(session).revoke_all_for_user(user, NOW, "password_reset")
            await session.rollback()
        assert await db.scalar("select revoked_at from extension_sessions where id = :id", id=session_id) is None
        async with db.sessions() as session:
            await ExtensionSessionPersistence(session).revoke_all_for_user(user, NOW, "password_reset")
            await session.commit()
        assert await db.scalar("select revoked_reason from extension_sessions where id = :id", id=session_id) == "password_reset"

    scenario(body)


def log_entry(user_id: uuid.UUID, surface: str) -> CallLogEntry:
    return CallLogEntry(
        key=UsageKey(user_id, date(2026, 11, 15), "groq", "m"),
        mode="ask",
        outcome="completed",
        tokens=TokenCounts(10, 5),
        cached_tokens=0,
        exact_usage=True,
        sizes=PromptSizes(),
        first_output_ms=1,
        duration_ms=2,
        surface=surface,
    )


@needs_db
def test_the_call_log_records_the_surface() -> None:
    async def body(db: Db) -> None:
        user = await db.user()
        await db.usage.log_call(log_entry(user, "codelab"))
        await db.usage.log_call(log_entry(user, "vscode"))
        async with db.sessions() as session:
            surfaces = (await session.execute(text("select surface from assistant_call_log order by created_at, surface"))).scalars().all()
        assert sorted(surfaces) == ["codelab", "vscode"]

    scenario(body)


@needs_db
def test_codelab_keeps_logging_before_025_is_applied() -> None:
    async def body(db: Db) -> None:
        user = await db.user()
        await db.usage.log_call(log_entry(user, "codelab"))
        assert await db.scalar("select count(*) from assistant_call_log") == 1
        with pytest.raises(DatabaseError):
            await db.usage.log_call(log_entry(user, "vscode"))

    scenario(body, with_surface=False)


@needs_db
def test_token_totals_use_the_same_rolling_week_as_the_limit() -> None:
    async def body(db: Db) -> None:
        user, other = await db.user(), await db.user()
        day = date(2026, 11, 15)
        limits = DailyLimits(user_calls=1000, user_tokens=10**9, global_tokens=10**9)
        for offset, tokens in ((0, 100), (6, 200), (7, 400)):
            key = UsageKey(user, day - timedelta(days=offset), "groq", "m")
            await db.usage.reserve_call(key, limits, TokenCounts(tokens, 0))
        await db.usage.reserve_call(UsageKey(user, day, "anthropic", "h"), limits, TokenCounts(0, 50))
        await db.usage.reserve_call(UsageKey(other, day, "groq", "m"), limits, TokenCounts(999, 0))
        totals = await db.usage.token_totals(user, day)
        assert (totals.today, totals.week) == (150, 350)
        assert await db.usage.token_totals(uuid.uuid4(), day) == type(totals)(0, 0)

    scenario(body)


@needs_db
def test_a_dead_database_is_a_database_error_and_holds_nothing() -> None:
    async def body(db: Db) -> None:
        dead = SqlExtensionSessionStore(
            async_sessionmaker(create_async_engine("postgresql+asyncpg://nobody:x@127.0.0.1:1/none", poolclass=NullPool)), timeout_seconds=3
        )
        with pytest.raises(DatabaseError):
            await dead.session_user(uuid.uuid4())
        with pytest.raises(DatabaseError):
            await dead.refresh("a", "b", NOW, GRACE, LIFETIME, None)

    scenario(body)


@needs_db
def test_a_session_read_never_waits_past_its_time_limit() -> None:
    async def body(db: Db) -> None:
        user = await db.user()
        session_id = await db.session_for(user)
        locker = await asyncpg.connect(TEST_DB_URL)
        try:
            await locker.execute("BEGIN; LOCK TABLE extension_sessions IN ACCESS EXCLUSIVE MODE;")
            quick = SqlExtensionSessionStore(db.sessions, timeout_seconds=0.5)
            with pytest.raises(DatabaseError):
                await quick.session_user(session_id)
        finally:
            await locker.execute("ROLLBACK")
            await locker.close()
        assert (await db.store.session_user(session_id)) is not None
        async with db.sessions() as session:
            assert (await session.execute(select(ExtensionSession.id))).scalar_one() == session_id

    scenario(body)
