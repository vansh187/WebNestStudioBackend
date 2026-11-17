"""Short lived brute-force limits for the website login route.

State is process-local, like the other lightweight application limiters. Use shared
storage before running multiple API workers/instances if limits must span them.
"""

import hashlib
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

ACCOUNT_ATTEMPT_LIMIT = 10
ACCOUNT_WINDOW_SECONDS = 15 * 60.0
_MAX_TRACKED_KEYS = 20_000


@dataclass(eq=False)
class _Attempt:
    started_at: float
    state: str = "pending"


class LoginRateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._account_attempts: dict[str, deque[_Attempt]] = {}

    def _account_key(self, email: str) -> str:
        # Match the exact identifier used by UserPersistence.get_by_email().
        normalized = email.strip()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def begin_attempt(self, email: str) -> tuple[_Attempt | None, int | None]:
        """Reserve a slot before authentication awaits, closing the concurrency gap."""
        now = self._clock()
        key = self._account_key(email)
        attempts = self._active(key, now)
        if len(attempts) >= ACCOUNT_ATTEMPT_LIMIT:
            return None, self._retry_after(attempts[0].started_at, now)
        attempt = _Attempt(now)
        attempts.append(attempt)
        self._trim()
        return attempt, None

    def finish_failure(self, email: str, attempt: _Attempt) -> None:
        """Keep a failed credential attempt in the window."""
        events = self._account_attempts.get(self._account_key(email))
        if events is not None and any(event is attempt for event in events):
            attempt.state = "failed"

    def finish_success(self, email: str, attempt: _Attempt) -> None:
        """Clear recorded failures on success while retaining other pending requests."""
        key = self._account_key(email)
        events = self._account_attempts.get(key)
        if events is None:
            return
        if any(event is attempt for event in events):
            attempt.state = "succeeded"
        pending = deque(event for event in events if event.state == "pending")
        if pending:
            self._account_attempts[key] = pending
        else:
            self._account_attempts.pop(key, None)

    def release_attempt(self, email: str, attempt: _Attempt) -> None:
        """Remove a reservation when authentication failed for a non-credential reason."""
        key = self._account_key(email)
        attempts = self._account_attempts.get(key)
        if attempts is not None:
            self._remove(attempts, attempt)
            if not attempts:
                self._account_attempts.pop(key, None)

    def _active(self, key: str, now: float) -> deque[_Attempt]:
        events = self._account_attempts.setdefault(key, deque())
        cutoff = now - ACCOUNT_WINDOW_SECONDS
        while events and events[0].started_at <= cutoff:
            events.popleft()
        if not events:
            self._account_attempts.pop(key, None)
            events = self._account_attempts.setdefault(key, deque())
        return events

    def _remove(self, events: deque[_Attempt], target: _Attempt) -> None:
        for event in events:
            if event is target:
                events.remove(event)
                return

    def _retry_after(self, oldest: float, now: float) -> int:
        return max(1, int(oldest + ACCOUNT_WINDOW_SECONDS - now + 0.999))

    def _trim(self) -> None:
        if len(self._account_attempts) > _MAX_TRACKED_KEYS:
            # Keep memory bounded under high email churn. In-memory limits are
            # best-effort under this pressure; shared storage is needed for strict
            # enforcement across high-volume or multi-worker deployments.
            excess = len(self._account_attempts) - _MAX_TRACKED_KEYS
            for key in list(self._account_attempts)[:excess]:
                self._account_attempts.pop(key, None)
