"""Runs code from code_output / spot_the_bug questions (plan 6.4 #5).

- python: the CodeLab sandbox (isolated interpreter, module allowlist).
- sql: an in-memory SQLite database with an authorizer, a size cap and a
  deadline. Output is the last statement's rows, one per line, columns joined
  by " | ", NULL printed as NULL.
- java / javascript: the coding playground's JDoodle path (external credits,
  counted against EXAM_CODE_RUN_DAILY_LIMIT by the caller).
Never raises for bad code: the outcome says it didn't run."""

import asyncio
import logging
import sqlite3
import time
from dataclasses import dataclass

from core.exceptions import DomainError
from schemas.coding_schemas import CodingFile
from services import codelab_sandbox
from services.codelab_sandbox import UnsafeCodeError

logger = logging.getLogger("webnest.exams.code")

RUN_TIMEOUT_SECONDS = 5.0
SQL_MAX_ROWS = 200
SQL_MAX_PAGES = 2000  # x 4 KB pages = 8 MB
SQL_MAX_SCRIPT_CHARS = 8000

_SQL_DENIED_ACTIONS = {
    sqlite3.SQLITE_ATTACH,
    sqlite3.SQLITE_DETACH,
    sqlite3.SQLITE_PRAGMA,
}
_SQL_DENIED_FUNCTIONS = {"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer"}


@dataclass(frozen=True)
class RunOutcomeData:
    ran: bool
    output: str
    error: str = ""


def _split_sql(script: str) -> list[str]:
    statements, buffer = [], ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            if buffer.strip():
                statements.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        statements.append(buffer.strip())
    return statements


def _run_sql_blocking(script: str, timeout_seconds: float) -> RunOutcomeData:
    if len(script) > SQL_MAX_SCRIPT_CHARS:
        return RunOutcomeData(False, "", "script is too long")
    deadline = time.monotonic() + timeout_seconds
    connection = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
    try:
        connection.execute(f"PRAGMA max_page_count = {SQL_MAX_PAGES}")

        def authorizer(action, arg1, arg2, db_name, trigger):
            if action in _SQL_DENIED_ACTIONS:
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in _SQL_DENIED_FUNCTIONS:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorizer)
        connection.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
        rows: list[tuple] = []
        for statement in _split_sql(script):
            cursor = connection.execute(statement)
            if cursor.description is not None:
                rows = cursor.fetchmany(SQL_MAX_ROWS + 1)
                if len(rows) > SQL_MAX_ROWS:
                    return RunOutcomeData(False, "", "too many result rows")
        lines = [" | ".join("NULL" if value is None else str(value) for value in row) for row in rows]
        return RunOutcomeData(True, "\n".join(lines))
    except sqlite3.Error as exc:
        if time.monotonic() > deadline:
            return RunOutcomeData(False, "", f"timed out after {timeout_seconds:g}s")
        return RunOutcomeData(False, "", f"sql error: {str(exc)[:200]}")
    except Exception as exc:
        return RunOutcomeData(False, "", f"could not run the script: {type(exc).__name__}")
    finally:
        connection.close()


class ExamCodeRunner:
    def __init__(self, settings) -> None:
        self._settings = settings

    def _jdoodle_configured(self) -> bool:
        return bool(self._settings.jdoodle_client_id.strip() and self._settings.jdoodle_client_secret.strip())

    def supports(self, language: str) -> bool:
        if language in ("python", "sql"):
            return True
        return language in ("java", "javascript") and self._jdoodle_configured()

    def is_external(self, language: str) -> bool:
        return language in ("java", "javascript")

    async def run(self, language: str, code: str) -> RunOutcomeData:
        try:
            if language == "python":
                result = await codelab_sandbox.run_python(code, "", RUN_TIMEOUT_SECONDS)
                return RunOutcomeData(result.ok, result.stdout, result.error)
            if language == "sql":
                return await asyncio.to_thread(_run_sql_blocking, code, RUN_TIMEOUT_SECONDS)
            if language in ("java", "javascript") and self._jdoodle_configured():
                return await self._run_jdoodle(language, code)
            return RunOutcomeData(False, "", f"{language} cannot be run")
        except UnsafeCodeError as exc:
            return RunOutcomeData(False, "", f"code not allowed: {str(exc)[:200]}")
        except Exception as exc:
            logger.warning("Verification run failed for %s", language, exc_info=True)
            return RunOutcomeData(False, "", f"run failed: {type(exc).__name__}")

    async def _run_jdoodle(self, language: str, code: str) -> RunOutcomeData:
        # Same engine call the coding playground uses; no session is needed
        # for the call itself (nothing is recorded per learner).
        from services.coding_service import CodingService

        service = CodingService(session=None, settings=self._settings)
        file_name = "Main.java" if language == "java" else "main.js"
        files = [CodingFile(name=file_name, content=code)]
        started = time.perf_counter()
        try:
            data = await asyncio.wait_for(
                service._call_jdoodle(language, None, files, ""), timeout=self._settings.compiler_request_timeout_seconds + 5
            )
            result = service._map_jdoodle_response(language, data, int((time.perf_counter() - started) * 1000))
        except DomainError as exc:
            return RunOutcomeData(False, "", f"engine error: {exc.message[:200]}")
        except (asyncio.TimeoutError, TimeoutError):
            return RunOutcomeData(False, "", "engine timed out")
        if result.status != "success":
            return RunOutcomeData(False, result.stdout, result.status)
        return RunOutcomeData(True, result.stdout)
