"""The RateLimiter the service sees: the in-memory minute window first (cheap),
then the per-user and all-users daily quotas in the database."""

import logging
import uuid
from datetime import datetime

from codelab_relay.errors import RelayDisabled, RelayError
from codelab_relay.infra.system_clock import next_utc_midnight
from codelab_relay.protocols import DailyCounter, MinuteLimiter

logger = logging.getLogger("webnest.codelab_relay")


class CompositeLimiter:
    def __init__(self, minute: MinuteLimiter, daily: DailyCounter) -> None:
        self._minute = minute
        self._daily = daily

    async def consume(self, user_id: uuid.UUID, now: datetime) -> int:
        try:
            left_this_minute = await self._minute.take(user_id, now)
            try:
                await self._daily.take(user_id, now)
            except RelayError:
                # Refused by the day quota: the call never happens, so it
                # doesn't count against the minute either.
                await self._minute.give_back(user_id, now)
                raise
            return left_this_minute
        except RelayError:
            raise
        except Exception as exc:
            logger.exception("rate limit check failed user=%s", user_id)
            raise RelayDisabled(temporary=True) from exc

    async def refund(self, user_id: uuid.UUID, now: datetime) -> None:
        """The relay failed after the quota was taken (DNS, timeout, provider
        unreachable, blocked redirect or oversized answer): give back the
        day's call so an outage can't drain everyone's daily quota. The
        minute slot stays used, which keeps retry storms throttled."""
        try:
            await self._daily.give_back(user_id, now)
        except RelayError:
            raise
        except Exception as exc:
            logger.exception("rate limit refund failed user=%s", user_id)
            raise RelayDisabled(temporary=True) from exc

    async def remaining(self, user_id: uuid.UUID, now: datetime) -> dict:
        try:
            return {
                "minute_remaining": await self._minute.remaining(user_id, now),
                "day_remaining": await self._daily.remaining(user_id, now),
                "day_resets_at": next_utc_midnight(now),
            }
        except RelayError:
            raise
        except Exception as exc:
            logger.exception("rate limit lookup failed user=%s", user_id)
            raise RelayDisabled(temporary=True) from exc
