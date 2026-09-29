import asyncio
import hashlib
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone

import google.auth.transport.requests
import httpx
from google.oauth2 import service_account
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings
from core.exceptions import (
    BadRequestError,
    DomainError,
    ExternalServiceError,
    PayloadTooLargeError,
    RateLimitedError,
)
from database.java_playground_usage_persistence import JavaPlaygroundUsagePersistence
from database.models import User
from schemas.java_playground_schemas import JavaRunRequest
from services.coding_service import SlidingWindowLimiter

logger = logging.getLogger("webnest.java_playground")

PER_MINUTE = 6
PER_DAY = 100

NOT_CONFIGURED_MESSAGE = "Java execution is not configured."
UNAVAILABLE_MESSAGE = "Java runner is unavailable right now. Please try again shortly."
_TOKEN_REFRESH_MARGIN = timedelta(minutes=5)
# google-auth's own HTTP timeout is 120s; never let a visitor wait that long.
_TOKEN_REFRESH_TIMEOUT_SECONDS = 20.0
# After a failed refresh, fail fast for a moment instead of making every queued
# request wait out its own refresh attempt against a failing Google endpoint.
_TOKEN_FAILURE_BACKOFF_SECONDS = 15.0
_MAX_ERROR_TEXT_CHARS = 500
_MAX_LOG_FIELD_CHARS = 64


def _log_field(value: object) -> str:
    # Playground fields end up in our logs: keep them short and on one line.
    return repr(value)[:_MAX_LOG_FIELD_CHARS]


class _IdTokenProvider:
    """Mints and caches the Google ID token Cloud Run requires.

    Tokens last about an hour; one is reused until it has less than five
    minutes left. The refresh does blocking network I/O, so it runs in a
    worker thread under a lock so concurrent requests share one refresh.
    Every failure surfaces as ExternalServiceError, never a raw exception.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._credentials: service_account.IDTokenCredentials | None = None
        self._cache_key: str | None = None
        self._failed_until = 0.0

    async def get_token(self, key_json: str, audience: str) -> str:
        # Fingerprint rather than the key itself, so a rotated key or URL
        # rebuilds the credentials without holding a second copy of the secret.
        cache_key = hashlib.sha256(f"{audience}\n{key_json}".encode("utf-8", errors="replace")).hexdigest()
        async with self._lock:
            if self._cache_key != cache_key or self._credentials is None:
                self._credentials = self._build_credentials(key_json, audience)
                self._cache_key = cache_key
                self._failed_until = 0.0
            credentials = self._credentials
            if not self._needs_refresh(credentials):
                return credentials.token

            if time.monotonic() < self._failed_until:
                raise ExternalServiceError(UNAVAILABLE_MESSAGE)
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(credentials.refresh, google.auth.transport.requests.Request()),
                    timeout=_TOKEN_REFRESH_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError as exc:
                self._failed_until = time.monotonic() + _TOKEN_FAILURE_BACKOFF_SECONDS
                logger.error("Timed out minting a Google ID token for the Java playground")
                raise ExternalServiceError(UNAVAILABLE_MESSAGE) from exc
            except Exception as exc:
                # RefreshError / TransportError / signing errors. Only the type
                # is logged: messages can carry token-endpoint response bodies.
                self._failed_until = time.monotonic() + _TOKEN_FAILURE_BACKOFF_SECONDS
                logger.error("Could not mint a Google ID token for the Java playground: %s", type(exc).__name__)
                raise ExternalServiceError(UNAVAILABLE_MESSAGE) from exc

            token = credentials.token
            if not isinstance(token, str) or not token:
                self._failed_until = time.monotonic() + _TOKEN_FAILURE_BACKOFF_SECONDS
                logger.error("Google returned an empty ID token for the Java playground")
                raise ExternalServiceError(UNAVAILABLE_MESSAGE)
            return token

    def reset(self) -> None:
        self._credentials = None
        self._cache_key = None
        self._failed_until = 0.0

    @staticmethod
    def _build_credentials(key_json: str, audience: str) -> service_account.IDTokenCredentials:
        try:
            info = json.loads(key_json)
            if not isinstance(info, dict):
                raise ValueError("service-account key must be a JSON object")
            return service_account.IDTokenCredentials.from_service_account_info(info, target_audience=audience)
        except Exception as exc:
            # Never include the exception text: it can echo parts of the key.
            logger.critical(
                "JAVA_PLAYGROUND_INVOKER_KEY is not a valid service-account JSON key (%s)", type(exc).__name__
            )
            raise ExternalServiceError(NOT_CONFIGURED_MESSAGE) from exc

    @staticmethod
    def _needs_refresh(credentials: service_account.IDTokenCredentials) -> bool:
        expiry = credentials.expiry
        if not credentials.token or not isinstance(expiry, datetime):
            return True
        # google-auth stores expiry as a naive UTC datetime.
        if expiry.tzinfo is not None:
            expiry = expiry.astimezone(timezone.utc).replace(tzinfo=None)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        return expiry - now < _TOKEN_REFRESH_MARGIN


class JavaPlaygroundService:
    """Forwards Java runs to the private Cloud Run playground, behind per-visitor
    and site-wide limits that keep its bill at zero."""

    _minute_limiter = SlidingWindowLimiter()
    _day_limiter = SlidingWindowLimiter()
    _token_provider = _IdTokenProvider()
    _cap_warned_day: date | None = None

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._usage = JavaPlaygroundUsagePersistence(session)
        self._settings = settings

    async def run(self, payload: JavaRunRequest, user: User | None, client_ip: str | None) -> bytes:
        """Returns the playground's JSON response body exactly as received.

        Only DomainError subclasses leave this method, so the route always
        answers with a clean {"detail": ...} error instead of a 500.
        """
        visitor = "unknown"
        try:
            visitor = self._visitor_key(user, client_ip)
            return await self._run(payload, visitor)
        except DomainError:
            raise
        except Exception as exc:
            logger.exception("Unexpected error while running Java visitor=%s", visitor)
            raise ExternalServiceError(UNAVAILABLE_MESSAGE) from exc

    async def _run(self, payload: JavaRunRequest, visitor: str) -> bytes:
        base_url = (self._settings.java_playground_url or "").strip().rstrip("/")
        key_json = (self._settings.java_playground_invoker_key or "").strip()
        if not (base_url.startswith("https://") and key_json):
            if base_url or key_json:
                logger.critical("Java playground is misconfigured: JAVA_PLAYGROUND_URL must be an https:// URL and the key must be set")
            raise ExternalServiceError(NOT_CONFIGURED_MESSAGE)

        await self._check_visitor_limits(visitor)
        # Mint the token before counting the run, so a Google auth outage never
        # spends the site-wide budget on runs that were not sent.
        token = await self._token_provider.get_token(key_json, base_url)
        await self._check_daily_cap()

        # Only our own token and the four body fields go out - never the
        # visitor's Authorization header, cookies or IP.
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(
                timeout=self._settings.java_playground_timeout_seconds, follow_redirects=False
            ) as client:
                response = await client.post(f"{base_url}/api/run", json=payload.to_playground_body(), headers=headers)
        except httpx.TimeoutException as exc:
            logger.warning("Java playground timed out visitor=%s", visitor)
            raise ExternalServiceError(UNAVAILABLE_MESSAGE) from exc
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            logger.warning("Java playground unreachable visitor=%s error=%s", visitor, type(exc).__name__)
            raise ExternalServiceError(UNAVAILABLE_MESSAGE) from exc

        return self._handle_response(response, visitor)

    def _handle_response(self, response: httpx.Response, visitor: str) -> bytes:
        code = response.status_code
        if code == 200:
            try:
                data = response.json()
            except ValueError as exc:
                logger.error("Java playground returned invalid JSON visitor=%s", visitor)
                raise ExternalServiceError(UNAVAILABLE_MESSAGE) from exc
            if not isinstance(data, dict) or "status" not in data:
                logger.error("Java playground returned an unexpected body visitor=%s", visitor)
                raise ExternalServiceError(UNAVAILABLE_MESSAGE)
            logger.info(
                "Java run visitor=%s request_id=%s status=%s duration_ms=%s",
                visitor,
                _log_field(data.get("requestId")),
                _log_field(data.get("status")),
                _log_field(data.get("durationMs")),
            )
            return response.content

        if code == 429:
            logger.info("Java playground busy visitor=%s", visitor)
            raise RateLimitedError("The Java runner is busy. Try again in a few seconds.")
        logger.warning("Java playground returned HTTP %s visitor=%s", code, visitor)
        if code == 400:
            raise BadRequestError(self._error_text(response, "The Java runner rejected this request."))
        if code == 413:
            raise PayloadTooLargeError(self._error_text(response, "Submitted code is too large."))
        if code in (401, 403):
            logger.critical(
                "Java playground refused our ID token (HTTP %s): check JAVA_PLAYGROUND_INVOKER_KEY, "
                "JAVA_PLAYGROUND_URL and the invoker IAM binding",
                code,
            )
            # Drop the cached token so a fixed key/IAM setup takes effect on the next run.
            self._token_provider.reset()
            raise ExternalServiceError("Java runner is unavailable right now.")
        if code < 500:
            # 3xx / 404 / 405 / other 4xx: our URL or the playground's API changed.
            logger.error("Java playground gave an unexpected HTTP %s: check JAVA_PLAYGROUND_URL", code)
        raise ExternalServiceError(UNAVAILABLE_MESSAGE)

    async def _check_visitor_limits(self, visitor: str) -> None:
        if not await self._minute_limiter.consume(visitor, PER_MINUTE, 60):
            raise RateLimitedError("You've reached the Java run limit. Try again in a minute.")
        if not await self._day_limiter.consume(visitor, PER_DAY, int(timedelta(days=1).total_seconds())):
            raise RateLimitedError("You've reached the Java run limit. Try again tomorrow.")

    async def _check_daily_cap(self) -> None:
        today = datetime.now(timezone.utc).date()
        try:
            runs = await self._usage.increment(today)
        except Exception as exc:
            # Fail closed: without the counter we cannot prove we are under the
            # cap, and an uncounted run could cost money.
            logger.error("Could not update the Java playground daily counter: %s", type(exc).__name__)
            raise ExternalServiceError(UNAVAILABLE_MESSAGE) from exc
        if runs > self._settings.java_playground_daily_cap:
            if JavaPlaygroundService._cap_warned_day != today:
                JavaPlaygroundService._cap_warned_day = today
                logger.warning(
                    "Java playground daily cap of %s runs reached for %s; pausing runs until tomorrow (UTC)",
                    self._settings.java_playground_daily_cap,
                    today.isoformat(),
                )
            raise RateLimitedError("Java runs are paused for today. Try again tomorrow.")

    def _visitor_key(self, user: User | None, client_ip: str | None) -> str:
        user_id = getattr(user, "id", None)
        if user_id is not None:
            return f"user:{user_id}"
        return f"ip:{self._hash_ip(client_ip) or 'unknown'}"

    def _hash_ip(self, client_ip: str | None) -> str | None:
        # Same salted hash as CodingService._hash_ip, so the two can be correlated.
        if not client_ip:
            return None
        return hashlib.sha256(
            f"{self._settings.jwt_secret_key}:{client_ip}".encode("utf-8", errors="replace")
        ).hexdigest()

    @staticmethod
    def _error_text(response: httpx.Response, fallback: str) -> str:
        try:
            data = response.json()
        except ValueError:
            return fallback
        error = data.get("error") if isinstance(data, dict) else None
        if not isinstance(error, str) or not error.strip():
            return fallback
        return error.strip()[:_MAX_ERROR_TEXT_CHARS]
