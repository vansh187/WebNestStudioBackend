"""The only file importing pymysql. One connection per request, closed in
finally. The socket is opened to the address the resolver checked and TLS
uses the real host name; with an explicit TLS context PyMySQL refuses a
server that doesn't offer TLS instead of falling back to plain text.
local_infile is off, multi-statements are off, and rows are read unbuffered
so only max_rows + 1 ever leave the server."""

import asyncio
import logging
import socket
import ssl
from concurrent.futures import Executor
from functools import partial

import pymysql
import pymysql.cursors

from codelab_db.constants import LOGGER_NAME
from codelab_db.errors import ConnectFailed, DbError, QueryError, QueryTimeout
from codelab_db.protocols import Limits, ServerInfo, SqlResult, SqlStatement, Target
from codelab_db.services.json_values import RowCollector
from codelab_db.services.redaction import redact

logger = logging.getLogger(LOGGER_NAME)

PROGRAM_NAME = "webnest-codelab"
# Server errors meaning "the statement ran too long": MySQL max_execution_time,
# query interrupted, MariaDB max_statement_time.
TIMEOUT_CODES = (3024, 1317, 1969)


def is_client_error(code: int) -> bool:
    """CR_* codes (2000-2999) mean the connection itself failed. Server
    errors use 1000-1999 and, in MySQL 8 / MariaDB, 3000 and up."""
    try:
        return 2000 <= code < 3000
    except TypeError:
        return False


def tls_context(verify: bool, ca_file: str | None) -> ssl.SSLContext:
    try:
        context = ssl.create_default_context(cafile=ca_file or None)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        if not verify:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context
    except (OSError, ssl.SSLError, ValueError) as exc:
        raise ConnectFailed("the TLS settings could not be prepared") from exc


def _code(error: Exception) -> int:
    try:
        return int(error.args[0]) if error.args and isinstance(error.args[0], int) else 0
    except (TypeError, ValueError):
        return 0


def _text(error: Exception) -> str:
    try:
        if len(error.args) >= 2:
            return f"({error.args[0]}) {error.args[1]}"
        return str(error) or type(error).__name__
    except Exception:
        return type(error).__name__


class MySqlConnector:
    def __init__(self, executor: Executor, open_socket=None, connection_class=None) -> None:
        self._executor = executor
        self._open_socket = open_socket or socket.create_connection
        self._connection_class = connection_class or pymysql.connections.Connection

    async def run(self, target: Target, request: SqlStatement, limits: Limits) -> SqlResult:
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self._executor, partial(self._run_sync, target, request, limits))
        except DbError:
            raise
        except Exception as exc:
            logger.error("mysql run failed domain=%s type=%s", target.domain, type(exc).__name__)
            raise QueryError("The query could not be run.") from exc

    async def test(self, target: Target, limits: Limits) -> ServerInfo:
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self._executor, partial(self._test_sync, target, limits))
        except DbError:
            raise
        except Exception as exc:
            logger.error("mysql test failed domain=%s type=%s", target.domain, type(exc).__name__)
            raise ConnectFailed("the connection test could not be completed") from exc

    def _connect(self, target: Target, limits: Limits):
        sock = None
        try:
            io_timeout = limits.statement_timeout_seconds + 2
            connection = self._connection_class(
                host=target.host, port=target.port, user=target.user, password=target.password or "",
                database=target.database, charset="utf8mb4", connect_timeout=limits.connect_timeout_seconds,
                read_timeout=io_timeout, write_timeout=io_timeout, autocommit=False, local_infile=False,
                client_flag=0, ssl=tls_context(target.tls_verify, limits.ca_file),
                defer_connect=True, program_name=PROGRAM_NAME, cursorclass=pymysql.cursors.SSCursor,
            )
            sock = self._open_socket((target.address, target.port), limits.connect_timeout_seconds)
            connection.connect(sock=sock)
            return connection
        except DbError:
            _close_socket(sock)
            raise
        except pymysql.MySQLError as exc:
            _close_socket(sock)
            raise ConnectFailed(redact(_text(exc), target.password)) from exc
        except (OSError, ssl.SSLError) as exc:
            _close_socket(sock)
            logger.info("mysql connect failed domain=%s type=%s", target.domain, type(exc).__name__)
            raise ConnectFailed(redact(str(exc) or "the database could not be reached", target.password)) from exc
        except Exception as exc:
            _close_socket(sock)
            logger.warning("mysql connect failed domain=%s type=%s", target.domain, type(exc).__name__)
            raise ConnectFailed("the database could not be reached") from exc

    def _run_sync(self, target: Target, request: SqlStatement, limits: Limits) -> SqlResult:
        connection = self._connect(target, limits)
        committed = False
        cursor = None
        try:
            cursor = connection.cursor()
            self._set_timeout(cursor, limits)
            cursor.execute(request.sql, request.params)
            if cursor.description is None:
                result = SqlResult([], [], max(cursor.rowcount or 0, 0))
            else:
                columns = [column[0] for column in cursor.description]
                collector = RowCollector(limits.max_rows, limits.max_bytes)
                for row in cursor.fetchmany(limits.max_rows + 1):
                    if not collector.add(list(row)):
                        # Not committed: closing the connection rolls back any write.
                        raise collector.too_large({"columns": columns, "rows": collector.rows,
                                                   "row_count": len(collector.rows)})
                result = SqlResult(columns, collector.rows, len(collector.rows))
            self._drain(cursor)
            connection.commit()
            committed = True
            return result
        except DbError:
            raise
        except pymysql.MySQLError as exc:
            code = _code(exc)
            if code in TIMEOUT_CODES or code == 2013:   # 2013: lost connection = the read timeout fired
                raise QueryTimeout(limits.statement_timeout_seconds) from exc
            if is_client_error(code):
                raise ConnectFailed(redact(_text(exc), target.password)) from exc
            raise QueryError(redact(_text(exc), target.password)) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise QueryTimeout(limits.statement_timeout_seconds) from exc
        except (OSError, ssl.SSLError) as exc:
            raise ConnectFailed(redact(str(exc) or "the connection was lost", target.password)) from exc
        except Exception as exc:
            logger.warning("mysql query failed domain=%s type=%s", target.domain, type(exc).__name__)
            raise QueryError("The query could not be run.") from exc
        finally:
            _close(connection, committed, cursor)

    def _set_timeout(self, cursor, limits: Limits) -> None:
        """max_execution_time stops long SELECTs server-side. Some MySQL-
        compatible servers (PlanetScale/Vitess) don't have it; the socket read
        timeout still stops everything else."""
        try:
            cursor.execute(f"SET SESSION max_execution_time = {int(limits.statement_timeout_seconds * 1000)}")
        except pymysql.MySQLError as exc:
            if is_client_error(_code(exc)):
                raise   # a connection problem: mapped by the caller
            logger.debug("max_execution_time not supported code=%s", _code(exc))
        except (OSError, ssl.SSLError):
            raise       # mapped by the caller
        except Exception as exc:
            raise QueryError("The statement timeout could not be set.") from exc

    def _drain(self, cursor) -> None:
        """A stored procedure CALL returns more than one result; the rest must
        be read before COMMIT, or the server answers "commands out of sync"."""
        try:
            for _ in range(16):
                if not cursor.nextset():
                    return
        except pymysql.MySQLError:
            raise       # mapped by the caller
        except Exception as exc:
            raise QueryError("The rest of the result could not be read.") from exc

    def _test_sync(self, target: Target, limits: Limits) -> ServerInfo:
        connection = self._connect(target, limits)
        try:
            cursor = connection.cursor()
            cursor.execute("SELECT VERSION()")
            rows = cursor.fetchall()   # reads to the end, so the unbuffered result is finished
            return ServerInfo(str(rows[0][0]) if rows else "unknown")
        except pymysql.MySQLError as exc:
            raise ConnectFailed(redact(_text(exc), target.password)) from exc
        except Exception as exc:
            logger.warning("mysql test query failed domain=%s type=%s", target.domain, type(exc).__name__)
            raise ConnectFailed("the server version could not be read") from exc
        finally:
            _close(connection, True)


def _abandon_result(connection, cursor) -> None:
    """Marks an unfinished unbuffered result as done, so PyMySQL's cursor
    close and result finaliser don't try to read the rest of it from the
    socket that is about to be dropped."""
    try:
        for holder in (cursor, connection):
            result = getattr(holder, "_result", None) if holder is not None else None
            if result is not None and getattr(result, "unbuffered_active", False):
                result.unbuffered_active = False
        if cursor is not None and hasattr(cursor, "connection"):
            cursor.connection = None
    except Exception as exc:
        logger.debug("mysql result could not be detached type=%s", type(exc).__name__)


def _close(connection, clean: bool, cursor=None) -> None:
    """A clean close says goodbye to the server. After an error, or with an
    unbuffered result still pending, the socket is just dropped (the server
    then rolls back anything uncommitted)."""
    try:
        if clean:
            connection.close()
            return
        _abandon_result(connection, cursor)
        force_close = getattr(connection, "_force_close", None)
        if callable(force_close):
            force_close()
        else:
            connection.close()
    except Exception as exc:
        logger.debug("mysql connection close failed type=%s", type(exc).__name__)


def _close_socket(sock) -> None:
    try:
        if sock is not None:
            sock.close()
    except Exception as exc:
        logger.debug("mysql socket close failed type=%s", type(exc).__name__)
