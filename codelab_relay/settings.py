"""RelaySettings, read from RELAY_* environment variables (section 7)."""

import logging
import uuid
from functools import cached_property
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from codelab_relay.errors import RelayDisabled

logger = logging.getLogger("webnest.codelab_relay")

DEFAULT_PROVIDERS_FILE = str(Path(__file__).resolve().with_name("providers.json"))
DEFAULT_CORS_ORIGINS = "https://www.webneststudio.co.in,http://127.0.0.1:5173,http://localhost:4173"


def _split(text: str) -> list[str]:
    try:
        return [part.strip() for part in text.split(",") if part.strip()]
    except AttributeError as exc:
        logger.error("relay setting is not a comma separated string")
        raise RelayDisabled() from exc


class RelaySettings(BaseSettings):
    # Real environment variables only (Render sets them there). The .env
    # source of pydantic-settings 2.6 ignores env_prefix, so the app-wide
    # CORS_ORIGINS=* in .env would leak into the relay's cors_origins.
    model_config = SettingsConfigDict(env_prefix="RELAY_", env_file=None, extra="ignore", populate_by_name=True)

    # Kill switch: false answers every relayed call with 503 relay_disabled
    # and lists no providers.
    enabled: bool = True
    per_minute: int = 30
    per_day: int = 300
    global_per_day: int = 5000
    max_request_bytes: int = 1048576
    max_response_bytes: int = 2097152
    timeout_seconds: int = 20
    connect_timeout_seconds: int = 5
    providers_file: str = DEFAULT_PROVIDERS_FILE
    # Browser origins allowed to call the relay. Never "*".
    cors_origins: str = DEFAULT_CORS_ORIGINS
    # Comma separated; plain str fields because pydantic-settings 2.6 would
    # try to JSON-decode a list type from the environment.
    extra_live_patterns_text: str = Field(default="", validation_alias="RELAY_EXTRA_LIVE_PATTERNS")
    blocked_user_ids_text: str = Field(default="", validation_alias="RELAY_BLOCKED_USER_IDS")

    # The parsed values below are computed once per settings object (settings
    # never change after import), so a bad entry is warned about once, not
    # on every relayed call.

    @cached_property
    def extra_live_patterns(self) -> tuple[str, ...]:
        try:
            return tuple(pattern.lower() for pattern in _split(self.extra_live_patterns_text))
        except RelayDisabled:
            raise
        except Exception as exc:
            logger.exception("could not read RELAY_EXTRA_LIVE_PATTERNS")
            raise RelayDisabled() from exc

    @cached_property
    def blocked_user_ids(self) -> frozenset[uuid.UUID]:
        try:
            blocked = set()
            for value in _split(self.blocked_user_ids_text):
                try:
                    blocked.add(uuid.UUID(value))
                except ValueError:
                    logger.warning("ignoring RELAY_BLOCKED_USER_IDS entry that is not a user id: %r", value[:64])
            return frozenset(blocked)
        except RelayDisabled:
            raise
        except Exception as exc:
            logger.exception("could not read RELAY_BLOCKED_USER_IDS")
            raise RelayDisabled() from exc

    @cached_property
    def cors_origin_list(self) -> list[str]:
        try:
            return [origin for origin in _split(self.cors_origins) if origin != "*"]
        except RelayDisabled:
            raise
        except Exception as exc:
            logger.exception("could not read RELAY_CORS_ORIGINS")
            raise RelayDisabled() from exc
