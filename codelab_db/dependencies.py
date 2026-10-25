"""The only place database proxy classes are constructed.

Production safety: a driver that fails to import (a missing wheel on Render)
only makes that engine unavailable - the app still starts and the other
engines keep working. Bad DB_PROXY_* values fall back to the defaults with
the proxy switched off. A broken hosts.json does stop startup, like the
relay's providers.json: it ships with the code and is covered by tests."""

import importlib
import logging

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from core.dependencies import container, get_auth_service, get_db_session
from core.playground_requests import webnest_user_id
from codelab_db.constants import ENGINES, LOGGER_NAME
from codelab_db.errors import DbError, Disabled, LoginRequired
from codelab_db.infra.dns_host_resolver import DnsHostResolver
from codelab_db.infra.json_host_allowlist import load_host_allowlist
from codelab_db.infra.memory_limiter import MemoryLimiter
from codelab_db.infra.renewable_executor import RenewableExecutor
from codelab_db.infra.system_clock import SystemClock
from codelab_db.protocols import DbUser
from codelab_db.services.http_io import BodyReader
from codelab_db.services.query_service import QueryService
from codelab_db.services.redaction import install_credential_log_filter
from codelab_db.services.statement_guard import MongoGuard, MySqlGuard, PostgresGuard
from codelab_db.services.target_validator import TargetValidator
from codelab_db.settings import DbProxySettings
from services.auth_service import AuthService

logger = logging.getLogger(LOGGER_NAME)

CONNECTOR_CLASSES = {
    "postgres": ("codelab_db.infra.pg_connector", "PostgresConnector"),
    "mysql": ("codelab_db.infra.mysql_connector", "MySqlConnector"),
    "mongodb": ("codelab_db.infra.mongo_connector", "MongoConnector"),
}
EXPOSED_HEADERS = ("X-WebNest-DB-Error", "Retry-After")


def _load_settings() -> DbProxySettings:
    try:
        return DbProxySettings()
    except Exception:
        logger.critical("DB_PROXY_* settings are invalid; the database proxy stays switched off", exc_info=True)
        # model_construct: the defaults, without reading the bad environment again.
        return DbProxySettings.model_construct(enabled=False)


def _load_connectors(pool: RenewableExecutor) -> dict:
    try:
        loaded = {}
        for engine in ENGINES:
            module_name, class_name = CONNECTOR_CLASSES[engine]
            try:
                loaded[engine] = getattr(importlib.import_module(module_name), class_name)(pool)
            except Exception:
                logger.critical("the %s connector could not be loaded; %s connections are unavailable",
                                engine, engine, exc_info=True)
        return loaded
    except Exception:
        logger.critical("the database connectors could not be loaded", exc_info=True)
        return {}


db_settings = _load_settings()
host_allowlist = load_host_allowlist(db_settings.hosts_file)
install_credential_log_filter()

clock = SystemClock()
limiter = MemoryLimiter(db_settings.per_minute, db_settings.per_day)
# Its own pool, sized to the concurrency cap, so slow databases can never
# starve the threads the rest of the app uses.
executor = RenewableExecutor(db_settings.max_concurrent)
validator = TargetValidator(host_allowlist, DnsHostResolver(), dns_timeout_seconds=db_settings.connect_timeout_seconds)
guards = {"postgres": PostgresGuard(db_settings.max_sql_bytes), "mysql": MySqlGuard(db_settings.max_sql_bytes),
          "mongodb": MongoGuard()}
connectors = _load_connectors(executor)
query_service = QueryService(db_settings, validator, guards, connectors, limiter, clock, executor)


def get_query_service() -> QueryService:
    try:
        return query_service
    except NameError as exc:
        raise Disabled(temporary=True) from exc


def get_body_reader() -> BodyReader:
    try:
        return BodyReader(db_settings.max_request_bytes)
    except Exception as exc:
        logger.exception("database proxy body reader wiring failed")
        raise Disabled(temporary=True) from exc


def check_db_enabled() -> None:
    """Runs before the login check and before the body is read."""
    try:
        if not db_settings.enabled:
            raise Disabled()
    except DbError:
        raise
    except Exception as exc:
        raise Disabled(temporary=True) from exc


async def get_db_user(request: Request, auth_service: AuthService = Depends(get_auth_service),
                      session: AsyncSession = Depends(get_db_session)) -> DbUser:
    """The WebNest login from X-WebNest-Authorization (shared with the relay)."""
    try:
        user_id = await webnest_user_id(request, auth_service, session, container.settings.email_enabled,
                                        DbError, LoginRequired, lambda: Disabled(temporary=True))
        return DbUser(id=user_id)
    except DbError:
        raise
    except Exception as exc:
        logger.error("database proxy login check failed type=%s", type(exc).__name__)
        raise Disabled(temporary=True) from exc


def db_cors_options() -> dict:
    try:
        return {
            "allow_origins": db_settings.cors_origin_list,
            "allow_methods": ["GET", "POST", "OPTIONS"],
            "allow_headers": ["x-webnest-authorization", "content-type"],
            "expose_headers": list(EXPOSED_HEADERS),
            "allow_credentials": False,
            "max_age": 600,
        }
    except Exception as exc:
        logger.critical("could not build the database proxy CORS policy", exc_info=True)
        raise Disabled() from exc


def describe_proxy() -> dict:
    try:
        return {
            "enabled": db_settings.enabled,
            "engines": {engine: {**config, "available": engine in connectors}
                        for engine, config in host_allowlist.describe().items()},
            "limits": {"per_minute": db_settings.per_minute, "per_day": db_settings.per_day,
                       "statement_timeout_seconds": db_settings.statement_timeout_seconds,
                       "max_rows": db_settings.max_rows, "max_response_bytes": db_settings.max_response_bytes,
                       "max_sql_bytes": db_settings.max_sql_bytes},
        }
    except DbError:
        raise
    except Exception as exc:
        logger.exception("database proxy description failed")
        raise Disabled(temporary=True) from exc


def close_db_resources() -> None:
    try:
        executor.shutdown(wait=False, cancel_futures=True)   # rebuilt on next use
    except Exception:
        logger.warning("could not shut down the database proxy executor", exc_info=True)
