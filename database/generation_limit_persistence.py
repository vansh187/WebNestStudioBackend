import uuid
from datetime import datetime

from sqlalchemy import case, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from database.base_persistence import BasePersistence
from database.models import GenerationLimit


class GenerationLimitPersistence(BasePersistence):
    """CRUD access to the generation_limits table (one row per user)."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def get_by_user(self, user_id: uuid.UUID) -> GenerationLimit | None:
        result = await self._execute(select(GenerationLimit).where(GenerationLimit.user_id == user_id))
        return result.scalar_one_or_none()

    async def try_consume(
        self, user_id: uuid.UUID, limit_per_hour: int, now: datetime, window_cutoff: datetime
    ) -> bool:
        """Atomically increments the user's counter (resetting it first if the
        hourly window has rolled over) and returns whether the slot was granted.

        Implemented as a single INSERT ... ON CONFLICT DO UPDATE ... WHERE
        statement so concurrent requests from the same user can't both read
        the same pre-increment count and both proceed past the cap (the classic
        check-then-write race of a separate SELECT + UPDATE). If the WHERE
        condition on the DO UPDATE doesn't match - window still open and
        already at the cap - Postgres skips the update and RETURNING yields no
        row, which is how a bad-luck loser of the race is told "no slot" here.
        """
        reset_expr = GenerationLimit.window_start < window_cutoff
        stmt = (
            pg_insert(GenerationLimit)
            .values(user_id=user_id, count=1, window_start=now)
            .on_conflict_do_update(
                index_elements=[GenerationLimit.user_id],
                set_={
                    "count": case((reset_expr, 1), else_=GenerationLimit.count + 1),
                    "window_start": case((reset_expr, now), else_=GenerationLimit.window_start),
                },
                where=or_(reset_expr, GenerationLimit.count < limit_per_hour),
            )
            .returning(GenerationLimit.id)
        )
        result = await self._execute(stmt)
        await self._commit(conflict_message="Rate limit record already exists for this user")
        return result.first() is not None
