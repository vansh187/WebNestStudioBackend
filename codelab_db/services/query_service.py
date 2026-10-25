"""Orchestrates one database request: validate → limit → connect → run → shape.

Everything that can be refused without the network is checked first (kill
switch, blocked users, URL syntax and allowlist, blocked statements, rate
limits), so a refused request never touches DNS or the learner's database."""

import asyncio
import logging
import time
import uuid

from codelab_db.constants import LOGGER_NAME, SQL_ENGINES
from codelab_db.errors import Busy, DbError, Disabled, QueryError, QueryTimeout, ResultTooLarge, UserBlocked
from codelab_db.protocols import Capacity, Clock, DatabaseConnector, Limits, MongoResult, RateLimiter, SqlResult
from codelab_db.services.statement_guard import mongo_statement_type
from codelab_db.services.target_validator import TargetValidator
from codelab_db.settings import DbProxySettings

logger = logging.getLogger(LOGGER_NAME)

# Time on top of connect + statement before the whole request is given up on.
OVERALL_SLACK_SECONDS = 5


class QueryService:
    def __init__(self, settings: DbProxySettings, validator: TargetValidator, guards: dict,
                 connectors: dict[str, DatabaseConnector], limiter: RateLimiter, clock: Clock,
                 capacity: Capacity) -> None:
        self._settings = settings
        self._validator = validator
        self._guards = guards
        self._connectors = connectors
        self._limiter = limiter
        self._clock = clock
        self._capacity = capacity
        self._running = 0

    def _limits(self) -> Limits:
        try:
            return Limits(self._settings.statement_timeout_seconds, self._settings.connect_timeout_seconds,
                          self._settings.max_rows, self._settings.max_response_bytes, self._settings.ca_file or None)
        except Exception as exc:
            raise Disabled(temporary=True) from exc

    def _admit(self, user_id: uuid.UUID, engine: str) -> DatabaseConnector:
        try:
            if not self._settings.enabled:
                raise Disabled()
            if user_id in self._settings.blocked_user_ids:
                raise UserBlocked()
            connector = self._connectors.get(engine)
            if connector is None:
                raise Disabled(reason=f"{engine} connections are not available right now.")
            return connector
        except DbError:
            raise
        except Exception as exc:
            raise Disabled(temporary=True) from exc

    async def query(self, user_id: uuid.UUID, request) -> dict:
        started = time.perf_counter()
        engine = getattr(request, "engine", "?")
        try:
            connector = self._admit(user_id, engine)
            parsed = self._validator.parse(engine, request.url.get_secret_value())
            statement = self._guards[engine].check(request, parsed.database)
            statement_type = (statement.statement_type if engine in SQL_ENGINES
                              else f"{mongo_statement_type(statement.operation)}:{statement.operation}")
            self._limiter.check(user_id, self._clock.now())
            result = await self._guarded(user_id, connector.run, parsed, statement)
            body = self._body(result, started)
            self._log(user_id, engine, parsed.rule.domain, statement_type, "ok", started, body.get("row_count",
                                                                                                body.get("count")))
            return body
        except ResultTooLarge as exc:
            exc.extra["ms"] = _ms(started)
            self._log(user_id, engine, None, None, exc.code, started, None)
            raise
        except DbError as exc:
            self._log(user_id, engine, None, None, exc.code, started, None)
            raise
        except Exception as exc:
            # Type only: a driver message could carry a learner's credentials.
            logger.error("database query failed user=%s engine=%s type=%s", user_id, engine, type(exc).__name__)
            raise QueryError("The query could not be run.") from exc

    async def test(self, user_id: uuid.UUID, request) -> dict:
        started = time.perf_counter()
        engine = getattr(request, "engine", "?")
        try:
            connector = self._admit(user_id, engine)
            parsed = self._validator.parse(engine, request.url.get_secret_value())
            self._limiter.check(user_id, self._clock.now())
            info = await self._guarded(user_id, lambda target, _, limits: connector.test(target, limits), parsed,
                                       None)
            self._log(user_id, engine, parsed.rule.domain, "TEST", "ok", started, None)
            return {"ok": True, "server_version": info.server_version, "ms": _ms(started)}
        except DbError as exc:
            self._log(user_id, engine, None, "TEST", exc.code, started, None)
            raise
        except Exception as exc:
            logger.error("database connection test failed user=%s engine=%s type=%s", user_id, engine,
                         type(exc).__name__)
            raise QueryError("The connection could not be tested.") from exc

    def _acquire(self) -> None:
        """Busy when requests or worker threads are all taken. Threads still
        finishing a timed-out query count, so nothing queues behind them."""
        try:
            if self._running >= self._settings.max_concurrent or not self._capacity.has_room():
                raise Busy()
            self._running += 1
        except DbError:
            raise
        except Exception as exc:
            raise Disabled(temporary=True) from exc

    async def _guarded(self, user_id: uuid.UUID, call, parsed, statement):
        """Concurrency slot → DNS (its own budget) → count the query → the
        connector call (its own budget). Refusals before the database is
        reached (busy, host checks, DNS) don't use up the learner's quota."""
        acquired = False
        try:
            self._acquire()
            acquired = True
            limits = self._limits()
            target = await self._validator.resolve(parsed)
            self._limiter.consume(user_id, self._clock.now())
            overall = limits.connect_timeout_seconds + limits.statement_timeout_seconds + OVERALL_SLACK_SECONDS
            async with asyncio.timeout(overall):
                return await call(target, statement, limits)
        except DbError:
            raise
        except TimeoutError as exc:
            raise QueryTimeout(self._settings.statement_timeout_seconds) from exc
        except Exception as exc:
            logger.warning("database call failed type=%s", type(exc).__name__)
            raise QueryError("The query could not be run.") from exc
        finally:
            if acquired:
                self._running -= 1

    def _body(self, result, started: float) -> dict:
        try:
            if isinstance(result, SqlResult):
                return {"columns": result.columns, "rows": result.rows, "row_count": result.row_count,
                        "truncated": False, "ms": _ms(started)}
            if isinstance(result, MongoResult):
                body = {"documents": result.documents, "count": result.count, "truncated": False,
                        "ms": _ms(started)}
                if result.write is not None:
                    body["write"] = result.write
                return body
            raise QueryError("The database returned an unexpected result.")
        except DbError:
            raise
        except Exception as exc:
            raise QueryError("The result could not be shaped.") from exc

    def _log(self, user_id, engine: str, domain: str | None, statement_type: str | None, status: str,
             started: float, rows) -> None:
        """B5.3: user id, engine, host domain only, statement type, status,
        ms, row count. Never the URL, the SQL, params or documents."""
        try:
            logger.info("db user=%s engine=%s domain=%s type=%s status=%s ms=%d rows=%s", user_id, engine,
                        domain or "-", statement_type or "-", status, _ms(started), rows if rows is not None else "-")
        except Exception:
            logger.warning("could not log a database proxy call")


def _ms(started: float) -> int:
    try:
        return int((time.perf_counter() - started) * 1000)
    except (TypeError, ValueError):
        return 0
