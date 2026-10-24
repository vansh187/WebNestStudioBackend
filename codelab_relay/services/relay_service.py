"""Orchestrates one relayed call (section 3.5).

Order matters: the target, headers, test mode and rate limits are all checked
before any DNS lookup or network call, so a refused request costs nothing
upstream. If the relay itself then fails (DNS, timeout, provider unreachable,
blocked redirect, oversized answer) the day's quota is given back."""

import logging
import time
import uuid

from codelab_relay.constants import RELAY_METHODS
from codelab_relay.errors import (
    MethodNotAllowed,
    PayloadTooLarge,
    RedirectBlocked,
    RelayDisabled,
    RelayError,
    ResponseTooLarge,
    UpstreamTimeout,
    UpstreamUnavailable,
    UserBlocked,
)
from codelab_relay.protocols import (
    Clock,
    HostResolver,
    OutgoingRequest,
    Provider,
    RateLimiter,
    UpstreamClient,
    UpstreamResponse,
)
from codelab_relay.services.header_filter import HeaderFilter
from codelab_relay.services.target_parser import TargetParser
from codelab_relay.services.test_mode import TestModeRuleFactory
from codelab_relay.settings import RelaySettings

logger = logging.getLogger("webnest.codelab_relay")

# Failures after the quota was taken in which the learner got no provider answer.
_REFUNDED = (UpstreamTimeout, UpstreamUnavailable, RedirectBlocked, ResponseTooLarge)


class RelayService:
    def __init__(self, settings: RelaySettings, parser: TargetParser, headers: HeaderFilter,
                 rules: TestModeRuleFactory, limiter: RateLimiter, resolver: HostResolver,
                 upstream: UpstreamClient, clock: Clock) -> None:
        self._settings = settings
        self._parser = parser
        self._headers = headers
        self._rules = rules
        self._limiter = limiter
        self._resolver = resolver
        self._upstream = upstream
        self._clock = clock

    async def relay(self, user_id: uuid.UUID, method: str, host: str, path: str, query: str,
                    incoming_headers: dict[str, str], body: bytes) -> tuple[UpstreamResponse, Provider, int]:
        started = time.perf_counter()
        provider: Provider | None = None
        counted = False
        try:
            if not self._settings.enabled:
                raise RelayDisabled()
            if user_id in self._settings.blocked_user_ids:
                raise UserBlocked()
            if method not in RELAY_METHODS:
                raise MethodNotAllowed()
            if len(body) > self._settings.max_request_bytes:
                raise PayloadTooLarge(self._settings.max_request_bytes)
            provider, url = self._parser.parse(host, path, query)
            outgoing_headers = self._headers.outgoing(provider, incoming_headers)
            self._rules.for_provider(provider).check(provider, outgoing_headers, query, body)
            remaining = await self._limiter.consume(user_id, self._clock.now())
            counted = True
            addresses = await self._resolver.resolve_public(host)
            response = await self._upstream.send(
                OutgoingRequest(method, url, outgoing_headers, body, connect_address=addresses[0]),
                self._settings.max_response_bytes, self._settings.timeout_seconds)
            if 300 <= response.status < 400:
                raise RedirectBlocked(provider.id, response.status, provider.name)
            logger.info("relay user=%s provider=%s method=%s path=%s status=%s bytes=%s ms=%d",
                        user_id, provider.id, method, "/" + path, response.status, len(response.body),
                        (time.perf_counter() - started) * 1000)
            return response, provider, remaining
        except RelayError as exc:
            error = self._named(exc, provider)
            self._log_refusal(user_id, provider, method, path, error)
            if counted and isinstance(error, _REFUNDED):
                await self._refund_quietly(user_id)
            if error is exc:
                raise
            raise error from exc
        except Exception as exc:
            logger.exception("relay failed user=%s provider=%s method=%s", user_id,
                             provider.id if provider else "unknown", method)
            if counted:
                await self._refund_quietly(user_id)
            raise UpstreamUnavailable(provider.id if provider else "unknown",
                                      provider.name if provider else None) from exc

    def _named(self, error: RelayError, provider: Provider | None) -> RelayError:
        """The resolver and upstream client only know the host; name the
        provider in their errors ("Razorpay did not answer within 20 seconds.")."""
        try:
            if provider is None:
                return error
            if isinstance(error, UpstreamTimeout):
                return UpstreamTimeout(provider.id, self._settings.timeout_seconds, provider.name)
            if isinstance(error, UpstreamUnavailable):
                return UpstreamUnavailable(provider.id, provider.name)
            return error
        except Exception:
            logger.warning("could not name the provider in a relay error", exc_info=True)
            return error

    async def _refund_quietly(self, user_id: uuid.UUID) -> None:
        """Never replaces the error the learner is about to get."""
        try:
            await self._limiter.refund(user_id, self._clock.now())
        except Exception:
            logger.warning("relay quota refund failed user=%s", user_id, exc_info=True)

    def _log_refusal(self, user_id: uuid.UUID, provider: Provider | None, method: str, path: str,
                     error: RelayError) -> None:
        try:
            # The path is only logged once it passed validation (provider known).
            logger.info("relay refused user=%s provider=%s method=%s path=%s code=%s",
                        user_id, provider.id if provider else "-", method[:10],
                        "/" + path if provider else "-", error.code)
        except Exception:
            logger.warning("could not log a relay refusal", exc_info=True)
