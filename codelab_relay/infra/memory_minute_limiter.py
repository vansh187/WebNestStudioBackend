"""Per-user sliding-minute limit, in process memory (section 5). Exact on
Render's single instance; with more instances, swap in a Redis class for the
same MinuteLimiter protocol.

No asyncio.Lock: no method awaits anything, so on the single event loop each
call runs to completion without interleaving, which is all a lock would add.
A lock would also be bound to the first event loop that contends for it and
then raise on any other loop (a second lifespan in the same process)."""

import logging
import math
import uuid
from collections import deque
from datetime import datetime, timedelta

from codelab_relay.errors import RateLimited, RelayDisabled, RelayError

logger = logging.getLogger("webnest.codelab_relay")

WINDOW = timedelta(minutes=1)
SWEEP_EVERY = timedelta(minutes=5)


class MemoryMinuteLimiter:
    def __init__(self, per_minute: int) -> None:
        self._per_minute = per_minute
        self._events: dict[uuid.UUID, deque[datetime]] = {}
        self._next_sweep: datetime | None = None

    async def take(self, user_id: uuid.UUID, now: datetime) -> int:
        try:
            self._maybe_sweep(now)
            events = self._recent(user_id, now)
            if len(events) >= self._per_minute:
                retry_after = max(1, math.ceil((events[0] + WINDOW - now).total_seconds())) if events else 60
                raise RateLimited(retry_after, "minute", f"{self._per_minute} calls a minute")
            events.append(now)
            self._events[user_id] = events
            return self._per_minute - len(events)
        except RelayError:
            raise
        except Exception as exc:
            logger.exception("minute limiter failed user=%s", user_id)
            raise RelayDisabled(temporary=True) from exc

    async def give_back(self, user_id: uuid.UUID, now: datetime) -> None:
        try:
            events = self._events.get(user_id)
            if events:
                events.pop()
                if not events:
                    del self._events[user_id]
        except Exception as exc:
            logger.exception("minute limiter refund failed user=%s", user_id)
            raise RelayDisabled(temporary=True) from exc

    async def remaining(self, user_id: uuid.UUID, now: datetime) -> int:
        try:
            return max(0, self._per_minute - len(self._recent(user_id, now)))
        except Exception as exc:
            logger.exception("minute limiter lookup failed user=%s", user_id)
            raise RelayDisabled(temporary=True) from exc

    def _recent(self, user_id: uuid.UUID, now: datetime) -> deque[datetime]:
        try:
            events = self._events.get(user_id, deque())
            while events and events[0] <= now - WINDOW:
                events.popleft()
            if not events:
                self._events.pop(user_id, None)
            return events
        except Exception as exc:
            logger.exception("minute limiter prune failed user=%s", user_id)
            raise RelayDisabled(temporary=True) from exc

    def _maybe_sweep(self, now: datetime) -> None:
        """Drops idle users so memory stays bounded."""
        try:
            if self._next_sweep is not None and now < self._next_sweep:
                return
            self._next_sweep = now + SWEEP_EVERY
            for user_id in [key for key, events in self._events.items() if not events or events[-1] <= now - WINDOW]:
                del self._events[user_id]
        except Exception as exc:
            logger.exception("minute limiter sweep failed")
            raise RelayDisabled(temporary=True) from exc
