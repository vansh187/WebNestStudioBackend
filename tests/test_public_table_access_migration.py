"""Migration 019 against a real Postgres, because roles, grants and RLS only exist there. Uses
EXAMS_TEST_DATABASE_URL - a throwaway database, never the production one in .env - and is
skipped when it is not set. It needs a superuser, to create the roles Supabase would provide."""

import asyncio
import os
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

import asyncpg
import pytest

TEST_DB_URL = os.environ.get("EXAMS_TEST_DATABASE_URL", "")
MIGRATION = (Path(__file__).resolve().parent.parent / "migrations" / "019_close_public_table_access.sql").read_text(encoding="utf-8")
APP_ROLE = "webnest_test_app"
OTHER_ROLE = "webnest_test_other"

pytestmark = pytest.mark.skipif(not TEST_DB_URL, reason="EXAMS_TEST_DATABASE_URL is not set")

Check = Callable[[asyncpg.Connection, str], Awaitable[None]]

CREATE_ROLES = f"""
DO $$
DECLARE
    role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated', '{APP_ROLE}', '{OTHER_ROLE}'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
            EXECUTE format('CREATE ROLE %I NOSUPERUSER NOBYPASSRLS', role_name);
        END IF;
    END LOOP;
END $$;
GRANT USAGE, CREATE ON SCHEMA public TO anon, authenticated, {APP_ROLE}, {OTHER_ROLE};
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO anon, authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE {APP_ROLE} IN SCHEMA public GRANT ALL ON TABLES TO anon, authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE {APP_ROLE} IN SCHEMA public GRANT ALL ON SEQUENCES TO anon, authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE {APP_ROLE} IN SCHEMA public GRANT ALL ON FUNCTIONS TO anon, authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE {APP_ROLE} GRANT EXECUTE ON FUNCTIONS TO PUBLIC;
"""


def run(check: Check) -> None:
    """A table and function owned by a plain role, like the API's, opened to anon as Supabase
    does, then patched. The function is named after the table with a `fn_` prefix."""

    async def scenario() -> None:
        connection = await asyncpg.connect(TEST_DB_URL)
        table = f"access_{uuid.uuid4().hex}"
        try:
            await connection.execute(CREATE_ROLES)
            await connection.execute(f"SET ROLE {APP_ROLE}")
            await connection.execute(f"CREATE TABLE {table} (id serial PRIMARY KEY, secret text)")
            await connection.execute(f"INSERT INTO {table} (secret) VALUES ('kept')")
            await connection.execute(f"CREATE FUNCTION fn_{table}() RETURNS int LANGUAGE sql AS 'SELECT 1'")
            await connection.execute("RESET ROLE")
            await connection.execute(MIGRATION)
            await check(connection, table)
        finally:
            await connection.execute("RESET ROLE")
            await connection.execute(f"DROP FUNCTION IF EXISTS fn_{table}()")
            await connection.execute(f"DROP TABLE IF EXISTS {table}")
            await connection.close()

    asyncio.run(scenario())


async def rls_on(connection: asyncpg.Connection, table: str) -> bool:
    return await connection.fetchval("SELECT relrowsecurity FROM pg_class WHERE relname = $1", table)


@pytest.mark.parametrize("role", ["anon", "authenticated"])
@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM {table}",
        "INSERT INTO {table} (secret) VALUES ('x')",
        "UPDATE {table} SET secret = 'x'",
        "DELETE FROM {table}",
        "TRUNCATE {table}",
        "SELECT fn_{table}()",
    ],
)
def test_public_roles_are_refused_every_kind_of_access(role: str, statement: str) -> None:
    async def check(connection: asyncpg.Connection, table: str) -> None:
        await connection.execute(f"SET ROLE {role}")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute(statement.format(table=table))

    run(check)


def test_the_tables_owner_keeps_working_with_rls_on_and_no_policies() -> None:
    async def check(connection: asyncpg.Connection, table: str) -> None:
        assert await rls_on(connection, table) is True
        await connection.execute(f"SET ROLE {APP_ROLE}")
        await connection.execute(f"INSERT INTO {table} (secret) VALUES ('new')")
        await connection.execute(f"UPDATE {table} SET secret = 'changed' WHERE secret = 'new'")
        assert [row["secret"] for row in await connection.fetch(f"SELECT secret FROM {table} ORDER BY id")] == ["kept", "changed"]
        await connection.execute(f"DELETE FROM {table} WHERE secret = 'changed'")
        assert await connection.fetchval(f"SELECT count(*) FROM {table}") == 1
        assert await connection.fetchval(f"SELECT fn_{table}()") == 1

    run(check)


@pytest.mark.parametrize("creator", [APP_ROLE, None], ids=["the_apis_role", "the_role_that_ran_the_patch"])
def test_what_is_created_after_the_patch_is_not_opened_to_public_roles(creator: str | None) -> None:
    async def check(connection: asyncpg.Connection, _table: str) -> None:
        later = f"later_{uuid.uuid4().hex}"
        if creator is not None:
            await connection.execute(f"SET ROLE {creator}")
        await connection.execute(f"CREATE TABLE {later} (id serial PRIMARY KEY)")
        await connection.execute(f"CREATE FUNCTION fn_{later}() RETURNS int LANGUAGE sql AS 'SELECT 1'")
        await connection.execute("RESET ROLE")
        try:
            for role in ("anon", "authenticated"):
                assert await connection.fetchval("SELECT has_table_privilege($1, $2, 'SELECT')", role, later) is False
                assert await connection.fetchval("SELECT has_sequence_privilege($1, $2, 'USAGE')", role, f"{later}_id_seq") is False
                assert await connection.fetchval("SELECT has_function_privilege($1, $2, 'EXECUTE')", role, f"fn_{later}()") is False
        finally:
            await connection.execute(f"DROP FUNCTION fn_{later}()")
            await connection.execute(f"DROP TABLE {later}")

    run(check)


def test_running_it_again_changes_nothing_and_leaves_the_lock_timeout_alone() -> None:
    async def check(connection: asyncpg.Connection, table: str) -> None:
        await connection.execute("SET lock_timeout = '7s'")
        await connection.execute(MIGRATION)
        assert await rls_on(connection, table) is True
        assert await connection.fetchval("SELECT has_table_privilege('anon', $1, 'SELECT')", table) is False
        assert await connection.fetchval("SELECT current_setting('lock_timeout')") == "7s"
        await connection.execute("RESET lock_timeout")

    run(check)


def test_a_locked_table_is_skipped_says_to_run_again_and_the_next_run_picks_it_up() -> None:
    async def check(connection: asyncpg.Connection, table: str) -> None:
        await connection.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
        messages: list[str] = []
        connection.add_log_listener(lambda _connection, message: messages.append(str(message)))
        holder = await asyncpg.connect(TEST_DB_URL)
        try:
            transaction = holder.transaction()
            await transaction.start()
            await holder.execute(f"LOCK TABLE {table} IN ACCESS SHARE MODE")
            await connection.execute(MIGRATION)
            assert await rls_on(connection, table) is False
            await transaction.rollback()
        finally:
            await holder.close()
        await asyncio.sleep(0)
        assert any("was locked" in message and table in message and "Run this again" in message for message in messages)
        await connection.execute(MIGRATION)
        assert await rls_on(connection, table) is True

    run(check)


def test_a_table_it_may_not_change_is_reported_as_another_owners_not_as_run_again() -> None:
    async def scenario() -> None:
        connection = await asyncpg.connect(TEST_DB_URL)
        table = f"owned_{uuid.uuid4().hex}"
        messages: list[str] = []
        try:
            await connection.execute(CREATE_ROLES)
            await connection.execute(f"SET ROLE {OTHER_ROLE}")
            await connection.execute(f"CREATE TABLE {table} (id int)")
            await connection.execute("RESET ROLE")
            await connection.execute(f"GRANT CREATE ON SCHEMA public TO {APP_ROLE}")
            await connection.execute(f"SET ROLE {APP_ROLE}")
            connection.add_log_listener(lambda _connection, message: messages.append(str(message)))
            await connection.execute(MIGRATION.replace("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon, authenticated;", ""))
            await asyncio.sleep(0)
            assert any("another role owns the table" in message and table in message and "will not help" in message for message in messages)
            assert not any("Run this again" in message and table in message for message in messages)
        finally:
            await connection.execute("RESET ROLE")
            await connection.execute(f"DROP TABLE IF EXISTS {table}")
            await connection.close()

    asyncio.run(scenario())
