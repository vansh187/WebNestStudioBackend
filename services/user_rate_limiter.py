"""A per-user sliding window, in process memory. Used on the event loop only, with no await
between check and record, so it needs no lock."""

import time
import uuid
from collections import deque
from collections.abc import Callable

_MAX_TRACKED_USERS = 10_000
# When the table is full of users still inside their window, this many of the longest-tracked
# are forgotten at once, so the clean-up runs rarely instead of on every new user.
_FORGET_WHEN_FULL = 1_000


class UserRateLimiter:
    def __init__(self, limit: int, window_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._calls: dict[uuid.UUID, deque[float]] = {}

    def allow(self, user_id: uuid.UUID, now: float | None = None) -> bool:
        """Counts this call and says whether it is within the limit. A refused call isn't
        counted, so someone retrying while limited isn't kept out for longer."""
        now = self._clock() if now is None else now
        cutoff = now - self._window
        if user_id not in self._calls and len(self._calls) >= _MAX_TRACKED_USERS:
            self._make_room(cutoff)
        times = self._calls.setdefault(user_id, deque())
        while times and times[0] <= cutoff:
            times.popleft()
        if len(times) >= self._limit:
            return False
        times.append(now)
        return True

    def _make_room(self, cutoff: float) -> None:
        self._calls = {user: times for user, times in self._calls.items() if times and times[-1] > cutoff}
        if len(self._calls) >= _MAX_TRACKED_USERS:
            for user in list(self._calls)[:_FORGET_WHEN_FULL]:
                del self._calls[user]
