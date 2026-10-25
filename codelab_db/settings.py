"""DbProxySettings, read from DB_PROXY_* environment variables.

Real environment variables only, like the relay: the .env source of
pydantic-settings 2.6 ignores env_prefix, so .env keys such as CORS_ORIGINS
would leak in."""

import logging
import uuid
from functools import cached_property
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from codelab_db.constants import LOGGER_NAME
from codelab_db.errors import Disabled

logger = logging.getLogger(LOGGER_NAME)

DEFAULT_HOSTS_FILE = str(Path(__file__).resolve().with_name("hosts.json"))
# Production only. Local development adds its origins through
# DB_PROXY_CORS_ORIGINS, so a dev server is never trusted in production.
DEFAULT_CORS_ORIGINS = "https://www.webneststudio.co.in"


def _split(text: str) -> list[str]:
    try:
        return [part.strip() for part in text.split(",") if part.strip()]
    except AttributeError as exc:
        logger.error("database proxy setting is not a comma separated string")
        raise Disabled() from exc


class DbProxySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DB_PROXY_", env_file=None, extra="ignore", populate_by_name=True)

    # Kill switch. Off by default: the proxy answers 503 db_disabled until
    # DB_PROXY_ENABLED=true is set on Render.
    enabled: bool = False
    per_minute: int = Field(default=60, ge=1)
    per_day: int = Field(default=1000, ge=1)
    statement_timeout_seconds: int = Field(default=10, ge=1, le=60)
    connect_timeout_seconds: int = Field(default=5, ge=2, le=30)
    max_rows: int = Field(default=500, ge=1, le=10000)
    max_response_bytes: int = Field(default=2097152, ge=1024)
    max_sql_bytes: int = Field(default=20480, ge=64)
    max_request_bytes: int = Field(default=262144, ge=1024)
    # Queries running at the same time across all learners (also the size of
    # the proxy's own thread pool, so it can't starve the rest of the app).
    max_concurrent: int = Field(default=8, ge=1, le=64)
    hosts_file: str = DEFAULT_HOSTS_FILE
    # Extra CA bundle for certificate checks. Empty: the system's CAs.
    ca_file: str = ""
    cors_origins: str = DEFAULT_CORS_ORIGINS
    blocked_user_ids_text: str = Field(default="", validation_alias="DB_PROXY_BLOCKED_USER_IDS")

    @cached_property
    def blocked_user_ids(self) -> frozenset[uuid.UUID]:
        try:
            blocked = set()
            for value in _split(self.blocked_user_ids_text):
                try:
                    blocked.add(uuid.UUID(value))
                except ValueError:
                    logger.warning("ignoring DB_PROXY_BLOCKED_USER_IDS entry that is not a user id: %r", value[:64])
            return frozenset(blocked)
        except Disabled:
            raise
        except Exception as exc:
            logger.exception("could not read DB_PROXY_BLOCKED_USER_IDS")
            raise Disabled() from exc

    @cached_property
    def cors_origin_list(self) -> list[str]:
        try:
            return [origin for origin in _split(self.cors_origins) if origin != "*"]
        except Disabled:
            raise
        except Exception as exc:
            logger.exception("could not read DB_PROXY_CORS_ORIGINS")
            raise Disabled() from exc
