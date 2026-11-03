import logging
from datetime import date, datetime, timedelta, timezone

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings
from core.exceptions import (
    BadRequestError,
    DomainError,
    ExternalServiceError,
    PayloadTooLargeError,
    RateLimitedError,
)
from database.models import User
from database.spring_playground_usage_persistence import SpringPlaygroundUsagePersistence
from schemas.spring_playground_schemas import SpringRunRequest
from services.coding_service import SlidingWindowLimiter
from services.java_playground_service import _IdTokenProvider

logger = logging.getLogger("webnest.spring_playground")

NOT_CONFIGURED_MESSAGE = "Spring Boot runs are not configured."
UNAVAILABLE_MESSAGE = "The Spring Boot runner is unavailable right now. Please try again shortly."
_MAX_ERROR_TEXT_CHARS = 500
_MAX_LOG_FIELD_CHARS = 64


def _log_field(value: object) -> str:
    return repr(value)[:_MAX_LOG_FIELD_CHARS]


class SpringPlaygroundService:
    """Forwards Spring Boot project runs to the private Cloud Run playground, for logged-in users,
    behind per-user and site-wide limits. Like JavaPlaygroundService, but for POST /api/project/run."""

    _minute_limiter = SlidingWindowLimiter()
    _day_limiter = SlidingWindowLimiter()
    # Its own token cache: the token is minted for this service's URL, not the Java one.
    _token_provider = _IdTokenProvider()
    _cap_warned_day: date | None = None

    def __init__(self, session: AsyncSession, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._usage = SpringPlaygroundUsagePersistence(session)
        self._settings = settings
        self._transport = transport

    async def run(self, payload: SpringRunRequest, user: User) -> bytes:
        """Returns the playground's JSON response body exactly as received.

        Only DomainError subclasses leave this method, so the route always answers with a clean
        {"detail": ...} error instead of a 500.
        """
        visitor = "unknown"
        try:
            visitor = f"user:{user.id}"
            return await self._run(payload, visitor)
        except DomainError:
            raise
        except Exception as exc:
            logger.exception("Unexpected error while running a Spring Boot project visitor=%s", visitor)
            raise ExternalServiceError(UNAVAILABLE_MESSAGE) from exc

    async def _run(self, payload: SpringRunRequest, visitor: str) -> bytes:
        base_url = (self._settings.spring_playground_url or "").strip().rstrip("/")
        key_json = (self._settings.spring_playground_invoker_key or self._settings.java_playground_invoker_key or "").strip()
        if not (base_url.startswith("https://") and key_json):
            if base_url:
                logger.critical("Spring playground is misconfigured: SPRING_PLAYGROUND_URL must be an https:// URL and an invoker key must be set")
            raise ExternalServiceError(NOT_CONFIGURED_MESSAGE)

        await self._check_visitor_limits(visitor)
        # Mint the token before counting the run, so a Google auth outage never spends the budget.
        token = await self._token_provider.get_token(key_json, base_url)
        day = await self._check_daily_cap()

        # Only our own token and the project go out - never the user's Authorization header or IP.
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(
                timeout=self._settings.spring_playground_timeout_seconds, follow_redirects=False, transport=self._transport
            ) as client:
                response = await client.post(f"{base_url}/api/project/run", json=payload.to_playground_body(), headers=headers)
        except httpx.TimeoutException as exc:
            logger.warning("Spring playground timed out visitor=%s", visitor)
            raise ExternalServiceError(UNAVAILABLE_MESSAGE) from exc
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            logger.warning("Spring playground unreachable visitor=%s error=%s", visitor, type(exc).__name__)
            raise ExternalServiceError(UNAVAILABLE_MESSAGE) from exc

        if response.status_code == 429:
            # Every instance was busy, so nothing ran: give back what was counted for it.
            await self._give_back(visitor, day)
        return self._handle_response(response, visitor)

    async def _give_back(self, visitor: str, day: date) -> None:
        await self._minute_limiter.release(visitor)
        await self._day_limiter.release(visitor)
        try:
            await self._usage.decrement(day)
        except Exception as exc:
            # Best effort: the run was counted once, which only errs on the safe side.
            logger.warning("Could not give back a Spring playground run: %s", type(exc).__name__)

    def _handle_response(self, response: httpx.Response, visitor: str) -> bytes:
        code = response.status_code
        if code == 200:
            try:
                data = response.json()
            except ValueError as exc:
                logger.error("Spring playground returned invalid JSON visitor=%s", visitor)
                raise ExternalServiceError(UNAVAILABLE_MESSAGE) from exc
            if not isinstance(data, dict) or "status" not in data:
                logger.error("Spring playground returned an unexpected body visitor=%s", visitor)
                raise ExternalServiceError(UNAVAILABLE_MESSAGE)
            # Never log the project, the requests or the app's output.
            responses = data.get("responses")
            logger.info(
                "Spring run visitor=%s request_id=%s status=%s phase=%s requests=%s duration_ms=%s",
                visitor,
                _log_field(data.get("requestId")),
                _log_field(data.get("status")),
                _log_field(data.get("phase")),
                len(responses) if isinstance(responses, list) else 0,
                _log_field(data.get("durationMs")),
            )
            return response.content

        if code == 429:
            logger.info("Spring playground busy visitor=%s", visitor)
            raise RateLimitedError("The Spring Boot runner is busy. Try again in a few seconds.")
        logger.warning("Spring playground returned HTTP %s visitor=%s", code, visitor)
        if code == 400:
            raise BadRequestError(self._error_text(response, "The Spring Boot runner rejected this project."))
        if code == 413:
            raise PayloadTooLargeError(self._error_text(response, "The project is too large."))
        if code in (401, 403):
            logger.critical(
                "Spring playground refused our ID token (HTTP %s): check the invoker key, SPRING_PLAYGROUND_URL "
                "and the invoker IAM binding",
                code,
            )
            self._token_provider.reset()
            raise ExternalServiceError("The Spring Boot runner is unavailable right now.")
        if code < 500:
            logger.error("Spring playground gave an unexpected HTTP %s: check SPRING_PLAYGROUND_URL", code)
        raise ExternalServiceError(UNAVAILABLE_MESSAGE)

    async def _check_visitor_limits(self, visitor: str) -> None:
        per_minute = self._settings.spring_playground_per_minute
        per_day = self._settings.spring_playground_per_day
        if not await self._minute_limiter.consume(visitor, per_minute, 60):
            raise RateLimitedError("You've reached the Spring Boot run limit. Try again in a minute.")
        if not await self._day_limiter.consume(visitor, per_day, int(timedelta(days=1).total_seconds())):
            raise RateLimitedError("You've reached today's Spring Boot run limit. Try again tomorrow.")

    async def _check_daily_cap(self) -> date:
        """Counts this run against today's site-wide cap and returns the day it was counted for."""
        today = datetime.now(timezone.utc).date()
        try:
            runs = await self._usage.increment(today)
        except Exception as exc:
            # Fail closed: without the counter we cannot prove we are under the cap.
            logger.error("Could not update the Spring playground daily counter: %s", type(exc).__name__)
            raise ExternalServiceError(UNAVAILABLE_MESSAGE) from exc
        if runs > self._settings.spring_playground_daily_cap:
            if SpringPlaygroundService._cap_warned_day != today:
                SpringPlaygroundService._cap_warned_day = today
                logger.warning(
                    "Spring playground daily cap of %s runs reached for %s; pausing runs until tomorrow (UTC)",
                    self._settings.spring_playground_daily_cap,
                    today.isoformat(),
                )
            raise RateLimitedError("Spring Boot runs are paused for today. Try again tomorrow.")
        return today

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
