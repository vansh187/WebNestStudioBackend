"""Per-user limits in process memory: a sliding minute window and a UTC-day
count. Exact on Render's single instance. The day count restarts with the
process, which only ever errs towards allowing; a database table can replace
this class later through the same RateLimiter protocol.

No lock: nothing here awaits, so calls can't interleave on the event loop."""

import logging
import math
import uuid
from collections import deque
from datetime import date, datetime, time, timedelta, timezone

from codelab_db.constants import LOGGER_NAME
from codelab_db.errors import DbError, Disabled, RateLimited

logger = logging.getLogger(LOGGER_NAME)

WINDOW = timedelta(minutes=1)
SWEEP_EVERY = timedelta(minutes=5)


def seconds_until_midnight(now: datetime) -> int:
    try:
        midnight = datetime.combine(now.astimezone(timezone.utc).date() + timedelta(days=1), time(0),
                                    tzinfo=timezone.utc)
        return max(1, math.ceil((midnight - now).total_seconds()))
    except (OverflowError, ValueError) as exc:
        raise Disabled(temporary=True) from exc


class MemoryLimiter:
    def __init__(self, per_minute: int, per_day: int) -> None:
        self._per_minute = per_minute
        self._per_day = per_day
        self._minute: dict[uuid.UUID, deque[datetime]] = {}
        self._day: date | None = None
        self._day_counts: dict[uuid.UUID, int] = {}
        self._next_sweep: datetime | None = None

    def check(self, user_id: uuid.UUID, now: datetime) -> None:
        """Refuses a learner who is already at a limit, without counting."""
        try:
            self._recent(user_id, now)
        except DbError:
            raise
        except Exception as exc:
            logger.error("database proxy limiter check failed user=%s type=%s", user_id, type(exc).__name__)
            raise Disabled(temporary=True) from exc

    def consume(self, user_id: uuid.UUID, now: datetime) -> None:
        """Counts a query that is about to reach the database (and still
        refuses it if another request got there first)."""
        try:
            events = self._recent(user_id, now)
            events.append(now)
            self._minute[user_id] = events
            self._day_counts[user_id] = self._day_counts.get(user_id, 0) + 1
        except DbError:
            raise
        except Exception as exc:
            logger.error("database proxy limiter failed user=%s type=%s", user_id, type(exc).__name__)
            raise Disabled(temporary=True) from exc

    def _recent(self, user_id: uuid.UUID, now: datetime) -> deque:
        """This minute's calls for the user; raises RateLimited at a limit."""
        try:
            self._roll_day(now)
            self._maybe_sweep(now)
            if self._day_counts.get(user_id, 0) >= self._per_day:
                raise RateLimited(seconds_until_midnight(now), "day", f"{self._per_day} queries a day")
            events = self._minute.get(user_id, deque())
            while events and events[0] <= now - WINDOW:
                events.popleft()
            if len(events) >= self._per_minute:
                retry_after = max(1, math.ceil((events[0] + WINDOW - now).total_seconds()))
                raise RateLimited(retry_after, "minute", f"{self._per_minute} queries a minute")
            return events
        except DbError:
            raise
        except Exception as exc:
            raise Disabled(temporary=True) from exc

    def _roll_day(self, now: datetime) -> None:
        try:
            today = now.astimezone(timezone.utc).date()
            if self._day != today:
                self._day, self._day_counts = today, {}
        except Exception as exc:
            raise Disabled(temporary=True) from exc

    def _maybe_sweep(self, now: datetime) -> None:
        """Drops idle users so memory stays bounded."""
        try:
            if self._next_sweep is not None and now < self._next_sweep:
                return
            self._next_sweep = now + SWEEP_EVERY
            for user_id in [key for key, events in self._minute.items() if not events or events[-1] <= now - WINDOW]:
                del self._minute[user_id]
        except Exception as exc:
            raise Disabled(temporary=True) from exc
