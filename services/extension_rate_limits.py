"""Per-minute limits for the VS Code extension's routes, in process memory like the
assistant's one-stream rule: move them to shared storage before running more than one
instance. Login and refresh are per IP (before anyone is known), the rest per user."""

import time
import uuid
from collections.abc import Callable

from services.user_rate_limiter import UserRateLimiter

WINDOW_SECONDS = 60
# name: calls allowed per WINDOW_SECONDS.
DEFAULT_LIMITS: dict[str, int] = {
    "codes": 10,  # per user: 2.1
    "token": 20,  # per IP: 2.3
    "refresh": 20,  # per IP: 2.4
    "logout": 30,  # per IP; over it, logout still answers 204 but does nothing
    "me": 120,  # per user: polled at most every 10 s per window
    "chat": 30,  # per user, on top of the daily and weekly token caps
}


class ExtensionRateLimits:
    def __init__(self, limits: dict[str, int] | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        chosen = {**DEFAULT_LIMITS, **(limits or {})}
        self._limiters = {name: UserRateLimiter(max(calls, 1), float(WINDOW_SECONDS), clock) for name, calls in chosen.items()}

    def allow(self, name: str, key: uuid.UUID) -> bool:
        """Counts the call. An unknown name is a programming error, refused rather than let through."""
        limiter = self._limiters.get(name)
        return limiter is not None and limiter.allow(key)

    def window_seconds(self, name: str) -> int:
        """How long to wait at most before the limit frees a call."""
        return WINDOW_SECONDS
