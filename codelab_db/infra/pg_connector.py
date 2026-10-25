"""The only file importing psycopg. One connection per request, closed in
finally; TLS always (verify-full where the provider's certificates chain to
a public CA); connected to the address the resolver checked (hostaddr), with
the real host name still used for TLS.

Each statement runs in its own transaction with SET LOCAL statement_timeout,
which also holds on Supabase's transaction pooler (port 6543). Statements
that can't run in a transaction block, and transaction control, run in
autocommit with a session statement_timeout instead. SELECT-like
statements are read through a server-side cursor, so only max_rows + 1 rows
ever leave the server."""

import asyncio
import logging
import os
from concurrent.futures import Executor
from functools import partial

import psycopg
from psycopg import errors as pg_errors

from codelab_db.constants import LOGGER_NAME
from codelab_db.errors import ConnectFailed, DbError, QueryError, QueryTimeout
from codelab_db.protocols import Limits, ServerInfo, SqlResult, SqlStatement, Target
from codelab_db.services.json_values import RowCollector
from codelab_db.services.redaction import redact

logger = logging.getLogger(LOGGER_NAME)

APPLICATION_NAME = "webnest-codelab"
CURSOR_NAME = "webnest_codelab"


class PostgresConnector:
    def __init__(self, executor: Executor, connect=None) -> None:
        self._executor = executor
        self._connect_fn = connect or psycopg.connect

    async def run(self, target: Target, request: SqlStatement, limits: Limits) -> SqlResult:
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self._executor, partial(self._run_sync, target, request, limits))
        except DbError:
            raise
        except Exception as exc:
            logger.error("postgres run failed domain=%s type=%s", target.domain, type(exc).__name__)
            raise QueryError("The query could not be run.") from exc

    async def test(self, target: Target, limits: Limits) -> ServerInfo:
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self._executor, partial(self._test_sync, target, limits))
        except DbError:
            raise
        except Exception as exc:
            logger.error("postgres test failed domain=%s type=%s", target.domain, type(exc).__name__)
            raise ConnectFailed("the connection test could not be completed") from exc

    def _connect(self, target: Target, limits: Limits):
        try:
            kwargs = dict(
                host=target.host, hostaddr=target.address, port=target.port, dbname=target.database or "postgres",
                user=target.user, password=target.password or "", connect_timeout=limits.connect_timeout_seconds,
                application_name=APPLICATION_NAME, prepare_threshold=None, autocommit=False,
                sslmode="verify-full" if target.tls_verify else "require",
                gssencmode="disable", passfile=os.devnull, keepalives=1, keepalives_idle=5,
                keepalives_interval=2, keepalives_count=3,
                tcp_user_timeout=(limits.statement_timeout_seconds + limits.connect_timeout_seconds) * 1000,
            )
            if target.tls_verify:
                kwargs["sslrootcert"] = limits.ca_file or "system"
            for key in ("channel_binding", "options"):
                if key in target.options:
                    kwargs[key] = target.options[key]
            return self._connect_fn(**kwargs)
        except psycopg.Error as exc:
            raise ConnectFailed(redact(_first_line(exc), target.password)) from exc
        except Exception as exc:
            logger.warning("postgres connect failed domain=%s type=%s", target.domain, type(exc).__name__)
            raise ConnectFailed("the database could not be reached") from exc

    def _run_sync(self, target: Target, request: SqlStatement, limits: Limits) -> SqlResult:
        connection = self._connect(target, limits)
        timeout_ms = int(limits.statement_timeout_seconds * 1000)
        try:
            if request.kind == "autocommit":
                # Can't run in a transaction block (VACUUM, ... CONCURRENTLY,
                # CREATE DATABASE) or is transaction control itself.
                connection.autocommit = True
                connection.execute(f"SET statement_timeout = {timeout_ms}")
                cursor = connection.cursor()
                cursor.execute(request.sql, request.params)
                return self._collect(cursor, limits, read_only=False)
            with connection.transaction():
                connection.execute(f"SET LOCAL statement_timeout = {timeout_ms}")
                if request.kind == "cursor":
                    with connection.cursor(name=CURSOR_NAME) as cursor:
                        cursor.execute(request.sql, request.params)
                        return self._collect(cursor, limits, read_only=True)
                cursor = connection.cursor()
                cursor.execute(request.sql, request.params)
                return self._collect(cursor, limits, read_only=False)
        except DbError:
            raise
        except pg_errors.QueryCanceled as exc:
            raise QueryTimeout(limits.statement_timeout_seconds) from exc
        except psycopg.OperationalError as exc:
            raise ConnectFailed(redact(_first_line(exc), target.password)) from exc
        except psycopg.Error as exc:
            raise QueryError(redact(_message(exc), target.password)) from exc
        except Exception as exc:
            logger.warning("postgres query failed domain=%s type=%s", target.domain, type(exc).__name__)
            raise QueryError("The query could not be run.") from exc
        finally:
            _close(connection)

    def _collect(self, cursor, limits: Limits, read_only: bool) -> SqlResult:
        """Raises ResultTooLarge (rolling the transaction back) past a cap."""
        try:
            if cursor.description is None:
                return SqlResult([], [], max(cursor.rowcount, 0))
            columns = [column.name for column in cursor.description]
            collector = RowCollector(limits.max_rows, limits.max_bytes)
            for row in cursor.fetchmany(limits.max_rows + 1):
                if not collector.add(list(row)):
                    raise collector.too_large({"columns": columns, "rows": collector.rows,
                                               "row_count": len(collector.rows)})
            row_count = len(collector.rows) if read_only else max(cursor.rowcount, len(collector.rows))
            return SqlResult(columns, collector.rows, row_count)
        except DbError:
            raise
        except psycopg.Error:
            raise
        except Exception as exc:
            raise QueryError("The result could not be read.") from exc

    def _test_sync(self, target: Target, limits: Limits) -> ServerInfo:
        connection = self._connect(target, limits)
        try:
            row = connection.execute("SHOW server_version").fetchone()
            return ServerInfo(str(row[0]) if row else "unknown")
        except psycopg.Error as exc:
            raise ConnectFailed(redact(_first_line(exc), target.password)) from exc
        except Exception as exc:
            logger.warning("postgres test query failed domain=%s type=%s", target.domain, type(exc).__name__)
            raise ConnectFailed("the server version could not be read") from exc
        finally:
            _close(connection)


def _first_line(error: Exception) -> str:
    try:
        lines = [line for line in str(error).splitlines() if line.strip()]
        return lines[-1] if lines else type(error).__name__
    except Exception:
        return "connection failed"


def _message(error: Exception) -> str:
    """The server's error text, with its position marker lines kept."""
    try:
        text = str(error).strip()
        return text or type(error).__name__
    except Exception:
        return "The database returned an error."


def _close(connection) -> None:
    try:
        connection.close()
    except Exception as exc:
        logger.debug("postgres connection close failed type=%s", type(exc).__name__)
