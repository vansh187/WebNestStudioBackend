import asyncio
import logging
import re
import time
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
from database.node_playground_usage_persistence import NodePlaygroundUsagePersistence
from schemas.node_playground_schemas import NodeProjectRunRequest, NodeRunRequest
from services.coding_service import SlidingWindowLimiter
from services.java_playground_service import _IdTokenProvider

logger = logging.getLogger("webnest.node_playground")

NOT_CONFIGURED_MESSAGE = "Node.js runs are not configured."
UNAVAILABLE_MESSAGE = "The Node.js runner is unavailable right now. Please try again shortly."
_MAX_ERROR_TEXT_CHARS = 500
_MAX_LOG_FIELD_CHARS = 64
_MAX_LOGGED_MISSING = 10
_PACKAGES_TTL_SECONDS = 3600
# A failed lookup is remembered briefly, so a public page cannot call the runner on every view.
_PACKAGES_RETRY_SECONDS = 60
# Package names come from a learner's package.json: only plain names are logged, never control characters.
_PACKAGE_NAME = re.compile(r"[@\w./-]{1,80}", re.ASCII)
_PACKAGES_TIMEOUT_SECONDS = 20.0


def _log_field(value: object) -> str:
    return repr(value)[:_MAX_LOG_FIELD_CHARS]


class NodePlaygroundService:
    """Forwards Node.js scripts (POST /api/node/run) and Express apps (POST /api/node/project/run)
    to the private Cloud Run playground, for logged-in users, behind per-user and site-wide
    limits shared by both. Like SpringPlaygroundService."""

    _minute_limiter = SlidingWindowLimiter()
    _day_limiter = SlidingWindowLimiter()
    # Its own token cache: the token is minted for this service's URL.
    _token_provider = _IdTokenProvider()
    _cap_warned_day: date | None = None
    # The preinstalled package list changes only with a deploy, so it is cached for an hour:
    # (when, base URL, body or None for a failed lookup).
    _packages_cache: tuple[float, str, bytes | None] | None = None
    _packages_lock: asyncio.Lock | None = None

    def __init__(self, session: AsyncSession, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._usage = NodePlaygroundUsagePersistence(session)
        self._settings = settings
        self._transport = transport

    async def run_script(self, payload: NodeRunRequest, user: User) -> bytes:
        return await self._guarded("/api/node/run", payload.to_playground_body(), user)

    async def run_project(self, payload: NodeProjectRunRequest, user: User) -> bytes:
        return await self._guarded("/api/node/project/run", payload.to_playground_body(), user)

    async def packages(self) -> bytes:
        """The playground's list of preinstalled packages. Never counts as a run."""
        try:
            return await self._packages()
        except DomainError:
            raise
        except Exception as exc:
            logger.exception("Unexpected error while listing Node.js packages")
            raise ExternalServiceError(UNAVAILABLE_MESSAGE) from exc

    async def _guarded(self, path: str, body: dict, user: User) -> bytes:
        """Only DomainError subclasses leave this method, so the route always answers with a
        clean {"detail": ...} error instead of a 500."""
        visitor = "unknown"
        try:
            visitor = f"user:{user.id}"
            return await self._run(path, body, visitor)
        except DomainError:
            raise
        except Exception as exc:
            logger.exception("Unexpected error while running Node.js visitor=%s", visitor)
            raise ExternalServiceError(UNAVAILABLE_MESSAGE) from exc

    def _target(self) -> tuple[str, str]:
        s = self._settings
        base_url = (s.node_playground_url or s.spring_playground_url or "").strip().rstrip("/")
        key_json = (s.node_playground_invoker_key or s.spring_playground_invoker_key or s.java_playground_invoker_key or "").strip()
        if not (base_url.startswith("https://") and key_json):
            if base_url:
                logger.critical("Node playground is misconfigured: NODE_PLAYGROUND_URL must be an https:// URL and an invoker key must be set")
            raise ExternalServiceError(NOT_CONFIGURED_MESSAGE)
        return base_url, key_json

    async def _run(self, path: str, body: dict, visitor: str) -> bytes:
        base_url, key_json = self._target()
        await self._check_visitor_limits(visitor)
        # Mint the token before counting the run, so a Google auth outage never spends the budget.
        token = await self._token_provider.get_token(key_json, base_url)
        day = await self._check_daily_cap()

        # Only our own token and the project go out - never the user's Authorization header or IP.
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(
                timeout=self._settings.node_playground_timeout_seconds, follow_redirects=False, transport=self._transport
            ) as client:
                response = await client.post(f"{base_url}{path}", json=body, headers=headers)
        except httpx.TimeoutException as exc:
            logger.warning("Node playground timed out visitor=%s", visitor)
            raise ExternalServiceError(UNAVAILABLE_MESSAGE) from exc
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            logger.warning("Node playground unreachable visitor=%s error=%s", visitor, type(exc).__name__)
            raise ExternalServiceError(UNAVAILABLE_MESSAGE) from exc

        if response.status_code == 429:
            # Every instance was busy, so nothing ran: give back what was counted for it.
            await self._give_back(visitor, day)
        return self._handle_response(response, visitor, path)

    async def _packages(self) -> bytes:
        base_url, key_json = self._target()
        # One lookup at a time: callers that arrive meanwhile get its answer from the cache.
        if NodePlaygroundService._packages_lock is None:
            NodePlaygroundService._packages_lock = asyncio.Lock()
        async with NodePlaygroundService._packages_lock:
            cached = NodePlaygroundService._packages_cache
            if cached and cached[1] == base_url:
                age = time.monotonic() - cached[0]
                if cached[2] is not None and age < _PACKAGES_TTL_SECONDS:
                    return cached[2]
                if cached[2] is None and age < _PACKAGES_RETRY_SECONDS:
                    raise ExternalServiceError(UNAVAILABLE_MESSAGE)
            try:
                body = await self._fetch_packages(base_url, key_json)
            except Exception:
                NodePlaygroundService._packages_cache = (time.monotonic(), base_url, None)
                raise
            NodePlaygroundService._packages_cache = (time.monotonic(), base_url, body)
            return body

    async def _fetch_packages(self, base_url: str, key_json: str) -> bytes:
        token = await self._token_provider.get_token(key_json, base_url)
        try:
            async with httpx.AsyncClient(timeout=_PACKAGES_TIMEOUT_SECONDS, follow_redirects=False, transport=self._transport) as client:
                response = await client.get(f"{base_url}/api/node/packages", headers={"Authorization": f"Bearer {token}"})
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            logger.warning("Node playground package list unreachable error=%s", type(exc).__name__)
            raise ExternalServiceError(UNAVAILABLE_MESSAGE) from exc
        if response.status_code in (401, 403):
            self._token_provider.reset()
        if response.status_code != 200:
            logger.warning("Node playground package list returned HTTP %s", response.status_code)
            raise ExternalServiceError(UNAVAILABLE_MESSAGE)
        try:
            data = response.json()
        except ValueError as exc:
            raise ExternalServiceError(UNAVAILABLE_MESSAGE) from exc
        if not isinstance(data, dict) or not isinstance(data.get("packages"), list):
            raise ExternalServiceError(UNAVAILABLE_MESSAGE)
        return response.content

    async def _give_back(self, visitor: str, day: date) -> None:
        await self._minute_limiter.release(visitor)
        await self._day_limiter.release(visitor)
        try:
            await self._usage.decrement(day)
        except Exception as exc:
            # Best effort: the run was counted once, which only errs on the safe side.
            logger.warning("Could not give back a Node playground run: %s", type(exc).__name__)

    def _handle_response(self, response: httpx.Response, visitor: str, path: str) -> bytes:
        code = response.status_code
        if code == 200:
            try:
                data = response.json()
            except ValueError as exc:
                logger.error("Node playground returned invalid JSON visitor=%s", visitor)
                raise ExternalServiceError(UNAVAILABLE_MESSAGE) from exc
            if not isinstance(data, dict) or "status" not in data:
                logger.error("Node playground returned an unexpected body visitor=%s", visitor)
                raise ExternalServiceError(UNAVAILABLE_MESSAGE)
            # Never log the code, the input or the output. Missing package names are logged so
            # the preinstalled list can grow from what learners ask for.
            responses = data.get("responses")
            missing = data.get("missing")
            missing_names = (
                [name for name in missing[:_MAX_LOGGED_MISSING] if isinstance(name, str) and _PACKAGE_NAME.fullmatch(name)]
                if isinstance(missing, list)
                else []
            )
            logger.info(
                "Node run visitor=%s kind=%s request_id=%s status=%s phase=%s requests=%s duration_ms=%s missing=%s",
                visitor,
                "project" if path.endswith("/project/run") else "script",
                _log_field(data.get("requestId")),
                _log_field(data.get("status")),
                _log_field(data.get("phase")),
                len(responses) if isinstance(responses, list) else 0,
                _log_field(data.get("durationMs")),
                ",".join(missing_names) or "-",
            )
            return response.content

        if code == 429:
            logger.info("Node playground busy visitor=%s", visitor)
            raise RateLimitedError("The Node.js runner is busy. Try again in a few seconds.")
        logger.warning("Node playground returned HTTP %s visitor=%s", code, visitor)
        if code == 400:
            raise BadRequestError(self._error_text(response, "The Node.js runner rejected this project."))
        if code == 413:
            raise PayloadTooLargeError(self._error_text(response, "The project is too large."))
        if code in (401, 403):
            logger.critical(
                "Node playground refused our ID token (HTTP %s): check the invoker key, NODE_PLAYGROUND_URL "
                "and the invoker IAM binding",
                code,
            )
            self._token_provider.reset()
            raise ExternalServiceError("The Node.js runner is unavailable right now.")
        if code == 404:
            logger.error("Node playground has no %s: deploy the runner with Node.js support", path)
        elif code < 500:
            logger.error("Node playground gave an unexpected HTTP %s: check NODE_PLAYGROUND_URL", code)
        raise ExternalServiceError(UNAVAILABLE_MESSAGE)

    async def _check_visitor_limits(self, visitor: str) -> None:
        per_minute = self._settings.node_playground_per_minute
        per_day = self._settings.node_playground_per_day
        if not await self._minute_limiter.consume(visitor, per_minute, 60):
            raise RateLimitedError("You've reached the Node.js run limit. Try again in a minute.")
        if not await self._day_limiter.consume(visitor, per_day, int(timedelta(days=1).total_seconds())):
            raise RateLimitedError("You've reached today's Node.js run limit. Try again tomorrow.")

    async def _check_daily_cap(self) -> date:
        """Counts this run against today's site-wide cap and returns the day it was counted for."""
        today = datetime.now(timezone.utc).date()
        try:
            runs = await self._usage.increment(today)
        except Exception as exc:
            # Fail closed: without the counter we cannot prove we are under the cap.
            logger.error("Could not update the Node playground daily counter: %s", type(exc).__name__)
            raise ExternalServiceError(UNAVAILABLE_MESSAGE) from exc
        if runs > self._settings.node_playground_daily_cap:
            if NodePlaygroundService._cap_warned_day != today:
                NodePlaygroundService._cap_warned_day = today
                logger.warning(
                    "Node playground daily cap of %s runs reached for %s; pausing runs until tomorrow (UTC)",
                    self._settings.node_playground_daily_cap,
                    today.isoformat(),
                )
            raise RateLimitedError("Node.js runs are paused for today. Try again tomorrow.")
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
