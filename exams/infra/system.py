"""Clock, randomness, ids, rate limiting and the alert sender."""

import logging
import math
import random
import secrets
import time
import uuid
from collections import deque
from datetime import datetime, timezone

logger = logging.getLogger("webnest.exams")


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class SystemRandomSource:
    """Draws use the OS CSPRNG so nobody can predict a paper or its option order."""

    def draw_random(self) -> random.Random:
        return random.SystemRandom()


class SecretsIdGenerator:
    def new_uuid(self) -> uuid.UUID:
        return uuid.uuid4()

    def choice(self, alphabet: str) -> str:
        return secrets.choice(alphabet)


class SlidingWindowRateLimiter:
    """Per-process sliding-window limiter (no Redis in v1). Memory stays
    bounded: empty keys are dropped and idle keys are swept periodically."""

    _SWEEP_INTERVAL_SECONDS = 300.0

    def __init__(self, monotonic=time.monotonic) -> None:
        self._monotonic = monotonic
        self._events: dict[str, deque[float]] = {}
        self._longest_window = 0
        self._next_sweep = monotonic() + self._SWEEP_INTERVAL_SECONDS

    def hit(self, key: str, limit: int, window_seconds: int) -> int:
        now = self._monotonic()
        self._longest_window = max(self._longest_window, window_seconds)
        events = self._events.setdefault(key, deque())
        cutoff = now - window_seconds
        while events and events[0] <= cutoff:
            events.popleft()
        if len(events) < limit:
            events.append(now)
            retry_after = 0
        else:
            retry_after = max(1, math.ceil(events[0] + window_seconds - now))
        if not events:
            self._events.pop(key, None)
        if now >= self._next_sweep:
            self._sweep(now)
        return retry_after

    def _sweep(self, now: float) -> None:
        self._next_sweep = now + self._SWEEP_INTERVAL_SECONDS
        expiry = now - self._longest_window
        for key in [k for k, events in self._events.items() if not events or events[-1] <= expiry]:
            del self._events[key]


class EmailAlertSender:
    """Admin alerts through the existing email service. Never raises: an alert
    that can't be sent is logged instead."""

    def __init__(self, email_service) -> None:
        self._email = email_service

    async def send(self, subject: str, body: str) -> None:
        try:
            await self._email.send_alert_email(subject, body)
        except Exception:
            logger.error("Could not send exam alert email: %s", subject, exc_info=True)
