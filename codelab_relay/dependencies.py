"""The only place relay classes are constructed (section 3.9).

Process-wide pieces (settings, registry, minute limiter, rules, the HTTP
client) are built once at import, so a bad providers.json or RELAY_* value
stops the app from starting. The daily counter opens its own short sessions
from the app's session factory. Tests replace these functions through
app.dependency_overrides."""

import logging
from datetime import timedelta
from functools import partial

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from core.dependencies import container, get_auth_service, get_db_session
from core.exceptions import UnauthorizedError
from database.session import Database
from codelab_relay.constants import RELAY_METHODS
from codelab_relay.errors import LoginRequired, MethodNotAllowed, RelayDisabled, RelayError
from codelab_relay.infra.composite_limiter import CompositeLimiter
from codelab_relay.infra.dns_host_resolver import DnsHostResolver
from codelab_relay.infra.httpx_upstream_client import HttpxUpstreamClient, create_shared_httpx_client
from codelab_relay.infra.json_provider_registry import load_provider_registry
from codelab_relay.infra.memory_minute_limiter import MemoryMinuteLimiter
from codelab_relay.infra.sql_daily_counter import SessionFactory, SqlDailyCounter
from codelab_relay.infra.system_clock import SystemClock
from codelab_relay.logging_filter import install_relay_log_filter
from codelab_relay.protocols import RelayUser
from codelab_relay.services.header_filter import HeaderFilter
from codelab_relay.services.http_io import BodyReader, ResponseBuilder
from codelab_relay.services.provider_list_service import ProviderListService
from codelab_relay.services.relay_service import RelayService
from codelab_relay.services.target_parser import TargetParser
from codelab_relay.services.test_mode import (
    GLOBAL_LIVE_PATTERNS,
    KeyPrefixRule,
    LivePatternScanner,
    SandboxHostRule,
    TestModeRuleFactory,
)
from codelab_relay.services.usage_service import UsageService
from codelab_relay.settings import RelaySettings
from services.auth_service import AuthService

logger = logging.getLogger("webnest.codelab_relay")

USAGE_RETENTION = timedelta(days=30)
EXPOSED_RELAY_HEADERS = ("X-WebNest-Relay-Error", "X-WebNest-Relay-Provider", "X-WebNest-Relay-Remaining", "Retry-After")

relay_settings = RelaySettings()
provider_registry = load_provider_registry(relay_settings.providers_file)
install_relay_log_filter(tuple(host for provider in provider_registry.all_enabled() for host in provider.hosts))

clock = SystemClock()
minute_limiter = MemoryMinuteLimiter(relay_settings.per_minute)
header_filter = HeaderFilter()
host_resolver = DnsHostResolver()
live_scanner = LivePatternScanner(GLOBAL_LIVE_PATTERNS + relay_settings.extra_live_patterns)
test_mode_rules = TestModeRuleFactory(KeyPrefixRule(live_scanner), SandboxHostRule(live_scanner))
# Builds its httpx client on first use and again after close or on a new event loop.
upstream_client = HttpxUpstreamClient(
    partial(create_shared_httpx_client, relay_settings.connect_timeout_seconds, relay_settings.timeout_seconds)
)


def _daily_counter(sessions: SessionFactory | None = None) -> SqlDailyCounter:
    try:
        return SqlDailyCounter(sessions or container.database.session_scope, relay_settings.per_day,
                               relay_settings.global_per_day)
    except Exception as exc:
        logger.exception("could not build the relay daily counter")
        raise RelayDisabled(temporary=True) from exc


def get_relay_service() -> RelayService:
    try:
        limiter = CompositeLimiter(minute_limiter, _daily_counter())
        return RelayService(relay_settings, TargetParser(provider_registry), header_filter, test_mode_rules,
                            limiter, host_resolver, upstream_client, clock)
    except RelayError:
        raise
    except Exception as exc:
        logger.exception("relay wiring failed")
        raise RelayDisabled(temporary=True) from exc


def get_usage_service() -> UsageService:
    try:
        return UsageService(CompositeLimiter(minute_limiter, _daily_counter()), clock)
    except RelayError:
        raise
    except Exception as exc:
        logger.exception("relay usage wiring failed")
        raise RelayDisabled(temporary=True) from exc


def get_provider_list_service() -> ProviderListService:
    try:
        return ProviderListService(relay_settings, provider_registry)
    except Exception as exc:
        logger.exception("relay provider list wiring failed")
        raise RelayDisabled(temporary=True) from exc


def get_body_reader() -> BodyReader:
    try:
        return BodyReader(relay_settings.max_request_bytes)
    except Exception as exc:
        logger.exception("relay body reader wiring failed")
        raise RelayDisabled(temporary=True) from exc


def get_response_builder() -> ResponseBuilder:
    try:
        return ResponseBuilder(header_filter)
    except Exception as exc:
        logger.exception("relay response builder wiring failed")
        raise RelayDisabled(temporary=True) from exc


def check_relay_open(request: Request) -> None:
    """Runs before the login check and before the body is read: a switched-off
    relay answers 503 and an unsupported method 405 without touching the
    database or streaming a body."""
    try:
        if not relay_settings.enabled:
            raise RelayDisabled()
        if request.method not in RELAY_METHODS:
            raise MethodNotAllowed()
    except RelayError:
        raise
    except Exception as exc:
        logger.exception("relay request pre-check failed")
        raise RelayDisabled(temporary=True) from exc


async def get_relay_user(request: Request, auth_service: AuthService = Depends(get_auth_service),
                         session: AsyncSession = Depends(get_db_session)) -> RelayUser:
    """The WebNest login from X-WebNest-Authorization, checked by the same
    AuthService.get_current_user that get_current_user uses, with login's own
    verification rule. The standard Authorization header belongs to the
    payment provider."""
    try:
        scheme, _, token = request.headers.get("x-webnest-authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise LoginRequired()
        user = await auth_service.get_current_user(token.strip())
        # Same rule as AuthService.login: verification is only required while
        # email is switched on (otherwise accounts are never verified).
        if not user.is_active or (container.settings.email_enabled and not user.is_verified):
            raise LoginRequired()
        relay_user = RelayUser(id=user.id)
        # Hand the connection back now: nothing later in the request uses this
        # session, and holding it through a 20 s upstream call would starve the pool.
        await session.close()
        return relay_user
    except RelayError:
        raise
    except (UnauthorizedError, ValueError) as exc:
        raise LoginRequired() from exc
    except Exception as exc:
        logger.exception("relay login check failed")
        raise RelayDisabled(temporary=True) from exc


def relay_cors_options() -> dict:
    """Section 2.4: the relay's own CORS policy, built from the enabled providers."""
    try:
        enabled = provider_registry.all_enabled()
        request_headers = sorted({header for provider in enabled for header in provider.forward_request_headers})
        response_headers = sorted({header for provider in enabled for header in provider.forward_response_headers})
        return {
            "allow_origins": relay_settings.cors_origin_list,
            "allow_methods": [*RELAY_METHODS, "OPTIONS"],
            "allow_headers": ["x-webnest-authorization", *request_headers],
            "expose_headers": [*EXPOSED_RELAY_HEADERS, *response_headers],
            "allow_credentials": False,
            "max_age": 600,
        }
    except Exception as exc:
        logger.critical("could not build the relay CORS policy", exc_info=True)
        raise RelayDisabled() from exc


async def purge_old_usage(database: Database) -> int:
    """Deletes daily quota rows older than 30 days."""
    try:
        return await _daily_counter(database.session_scope).purge_before((clock.now() - USAGE_RETENTION).date())
    except RelayError:
        raise
    except Exception as exc:
        logger.exception("relay usage purge failed")
        raise RelayDisabled(temporary=True) from exc


async def close_relay_resources() -> None:
    try:
        await upstream_client.aclose()
    except Exception:
        logger.warning("could not close the relay resources", exc_info=True)
