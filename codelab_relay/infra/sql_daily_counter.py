"""The only file using the ORM: per-user and all-users daily quotas (section 5).

Each increment is one atomic upsert whose UPDATE only fires while the count is
under the limit, so concurrent calls can never push a day over its quota and a
refused call isn't counted.

Every operation runs in its own short session and transaction: nothing here
shares the request's session, and no connection is held during the (slow)
upstream call. When the all-users cap refuses a call, rolling the transaction
back also undoes that user's increment."""

import logging
import uuid
from collections.abc import Callable
from datetime import date, datetime, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from codelab_relay.errors import RateLimited, RelayDisabled, RelayError
from codelab_relay.infra.system_clock import seconds_until_midnight
from codelab_relay.models import RelayDailyUsage, RelayGlobalDailyUsage

logger = logging.getLogger("webnest.codelab_relay")

SessionFactory = Callable[[], AsyncSession]


def utc_day(now: datetime) -> date:
    try:
        return now.astimezone(timezone.utc).date()
    except (OverflowError, ValueError) as exc:
        raise RelayDisabled(temporary=True) from exc


class SqlDailyCounter:
    def __init__(self, sessions: SessionFactory, per_day: int, global_per_day: int) -> None:
        self._sessions = sessions
        self._per_day = per_day
        self._global_per_day = global_per_day

    async def take(self, user_id: uuid.UUID, now: datetime) -> int:
        try:
            day = utc_day(now)
            async with self._sessions() as session:
                user_count = await self._increment(session, RelayDailyUsage, {"user_id": user_id}, day,
                                                   self._per_day, [RelayDailyUsage.user_id, RelayDailyUsage.day])
                if user_count is None:
                    await session.rollback()
                    raise RateLimited(seconds_until_midnight(now), "day", f"{self._per_day} calls a day")
                global_count = await self._increment(session, RelayGlobalDailyUsage, {}, day, self._global_per_day,
                                                     [RelayGlobalDailyUsage.day])
                if global_count is None:
                    await session.rollback()   # also undoes the user's increment above
                    logger.warning("relay global daily cap reached day=%s", day)
                    raise RateLimited(seconds_until_midnight(now), "global", "the CodeLab daily limit for all learners")
                await session.commit()
                return max(0, self._per_day - user_count)
        except RelayError:
            raise
        except Exception as exc:
            # Leaving the session block rolled the transaction back.
            logger.error("relay daily counter failed user=%s type=%s", user_id, type(exc).__name__, exc_info=True)
            raise RelayDisabled(temporary=True) from exc

    async def give_back(self, user_id: uuid.UUID, now: datetime) -> None:
        """Un-counts one call (the relay itself failed, so the learner got
        no provider answer). Never takes a count below zero."""
        try:
            day = utc_day(now)
            async with self._sessions() as session:
                await session.execute(
                    update(RelayDailyUsage)
                    .where(RelayDailyUsage.user_id == user_id, RelayDailyUsage.day == day, RelayDailyUsage.count > 0)
                    .values(count=RelayDailyUsage.count - 1, updated_at=func.now())
                )
                await session.execute(
                    update(RelayGlobalDailyUsage)
                    .where(RelayGlobalDailyUsage.day == day, RelayGlobalDailyUsage.count > 0)
                    .values(count=RelayGlobalDailyUsage.count - 1, updated_at=func.now())
                )
                await session.commit()
        except RelayError:
            raise
        except Exception as exc:
            logger.error("relay quota refund failed user=%s type=%s", user_id, type(exc).__name__, exc_info=True)
            raise RelayDisabled(temporary=True) from exc

    async def remaining(self, user_id: uuid.UUID, now: datetime) -> int:
        try:
            day = utc_day(now)
            async with self._sessions() as session:
                count = await session.scalar(
                    select(RelayDailyUsage.count).where(RelayDailyUsage.user_id == user_id, RelayDailyUsage.day == day)
                )
            return max(0, self._per_day - (count or 0))
        except RelayError:
            raise
        except Exception as exc:
            logger.error("relay usage lookup failed user=%s type=%s", user_id, type(exc).__name__, exc_info=True)
            raise RelayDisabled(temporary=True) from exc

    async def purge_before(self, cutoff: date) -> int:
        """Deletes quota rows older than cutoff; returns how many went."""
        try:
            async with self._sessions() as session:
                users = await session.execute(delete(RelayDailyUsage).where(RelayDailyUsage.day < cutoff))
                everyone = await session.execute(delete(RelayGlobalDailyUsage).where(RelayGlobalDailyUsage.day < cutoff))
                await session.commit()
                return (users.rowcount or 0) + (everyone.rowcount or 0)
        except Exception as exc:
            logger.error("relay usage purge failed type=%s", type(exc).__name__, exc_info=True)
            raise RelayDisabled(temporary=True) from exc

    async def _increment(self, session: AsyncSession, model, keys: dict, day: date, limit: int,
                         conflict_columns: list) -> int | None:
        """Adds one call to today's row and returns the new count, or None when
        the row is already at the limit (nothing is changed then)."""
        try:
            if limit <= 0:
                return None
            statement = (
                insert(model)
                .values(**keys, day=day, count=1)
                .on_conflict_do_update(
                    index_elements=conflict_columns,
                    set_={"count": model.count + 1, "updated_at": func.now()},
                    where=model.count < limit,
                )
                .returning(model.count)
            )
            return await session.scalar(statement)
        except Exception as exc:
            # The caller's session block rolls the transaction back on the way out.
            logger.error("relay counter upsert failed table=%s type=%s", model.__tablename__, type(exc).__name__,
                         exc_info=True)
            raise RelayDisabled(temporary=True) from exc
