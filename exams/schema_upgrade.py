"""Brings an existing database up to the exam feature's schema before
Base.metadata.create_all runs.

create_all creates missing tables but never alters existing ones. The course
exams add columns to courses / course_modules / lessons and composite unique
keys that exam_questions' foreign keys reference, so without this step the
ORM would query columns that don't exist (every course page fails) and
create_all would fail creating exam_questions. On a fresh database the
tables don't exist yet and create_all builds them complete, so every step
here is skipped.

Each step runs only when the catalog says it is missing, in its own short
transaction with a lock timeout, so a busy table delays startup by seconds
instead of blocking traffic. migrations/012_course_exams.sql holds the same
statements for applying by hand.
"""

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger("webnest.exams.schema")

LOCK_TIMEOUT = "5s"
ATTEMPTS = 3
RETRY_PAUSE_SECONDS = 2.0

# (table, column, statement)
COLUMN_STEPS: tuple[tuple[str, str, str], ...] = (
    ("courses", "exam_code", "ALTER TABLE courses ADD COLUMN IF NOT EXISTS exam_code text"),
    (
        "courses",
        "exam_enabled",
        "ALTER TABLE courses ADD COLUMN IF NOT EXISTS exam_enabled boolean NOT NULL DEFAULT false",
    ),
    ("courses", "exam_ready_at", "ALTER TABLE courses ADD COLUMN IF NOT EXISTS exam_ready_at timestamptz"),
    (
        "courses",
        "exam_admin_disabled",
        "ALTER TABLE courses ADD COLUMN IF NOT EXISTS exam_admin_disabled boolean NOT NULL DEFAULT false",
    ),
    ("course_modules", "key", "ALTER TABLE course_modules ADD COLUMN IF NOT EXISTS key text"),
    ("lessons", "key", "ALTER TABLE lessons ADD COLUMN IF NOT EXISTS key text"),
)

# (table, index name, statement). Names match the ORM's constraint names so a
# database built by create_all and one upgraded here look the same.
INDEX_STEPS: tuple[tuple[str, str, str], ...] = (
    ("courses", "uq_courses_exam_code", "CREATE UNIQUE INDEX IF NOT EXISTS uq_courses_exam_code ON courses (exam_code)"),
    (
        "course_modules",
        "uq_course_modules_course_id_id",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_course_modules_course_id_id ON course_modules (course_id, id)",
    ),
    (
        "course_modules",
        "uq_course_modules_course_key",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_course_modules_course_key ON course_modules (course_id, key)",
    ),
    (
        "lessons",
        "uq_lessons_module_id_id",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_lessons_module_id_id ON lessons (module_id, id)",
    ),
    (
        "lessons",
        "uq_lessons_course_key",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_lessons_course_key ON lessons (course_id, key)",
    ),
    ("lessons", "ix_lessons_key", "CREATE INDEX IF NOT EXISTS ix_lessons_key ON lessons (key)"),
)


class SchemaUpgradeError(Exception):
    """A required schema step could not be applied; startup must not continue."""


async def _catalog(engine: AsyncEngine) -> tuple[set[str], set[tuple[str, str]], set[str]]:
    async with engine.connect() as connection:
        tables = set(
            (
                await connection.execute(
                    text("SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")
                )
            ).scalars()
        )
        columns = {
            (row[0], row[1])
            for row in await connection.execute(
                text(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name IN ('courses', 'course_modules', 'lessons')"
                )
            )
        }
        indexes = set(
            (
                await connection.execute(
                    text("SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()")
                )
            ).scalars()
        )
    return tables, columns, indexes


async def _apply(engine: AsyncEngine, statement: str) -> None:
    for attempt in range(1, ATTEMPTS + 1):
        try:
            async with engine.begin() as connection:
                await connection.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
                await connection.execute(text(statement))
            return
        except DBAPIError as exc:
            if attempt == ATTEMPTS:
                raise SchemaUpgradeError(f"Could not apply schema step: {statement}") from exc
            logger.warning(
                "Schema step failed (attempt %d/%d), retrying: %s", attempt, ATTEMPTS, statement, exc_info=True
            )
            await asyncio.sleep(RETRY_PAUSE_SECONDS)


async def upgrade_schema(engine: AsyncEngine) -> list[str]:
    """Applies the missing steps and returns the statements it ran. Raises
    SchemaUpgradeError when a step cannot be applied."""
    try:
        tables, columns, indexes = await _catalog(engine)
    except SQLAlchemyError as exc:
        raise SchemaUpgradeError("Could not read the database catalog") from exc

    applied: list[str] = []
    for table, column, statement in COLUMN_STEPS:
        if table in tables and (table, column) not in columns:
            await _apply(engine, statement)
            applied.append(statement)
    for table, index_name, statement in INDEX_STEPS:
        if table in tables and index_name not in indexes:
            await _apply(engine, statement)
            applied.append(statement)
    if applied:
        logger.info("Applied %d exam schema step(s): %s", len(applied), "; ".join(applied))
    return applied
