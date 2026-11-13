from datetime import date

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from database.base_persistence import BasePersistence
from database.models import NodePlaygroundDailyUsage


class NodePlaygroundUsagePersistence(BasePersistence):
    """Access to the node_playground_daily_usage table (one row per UTC day)."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def increment(self, day: date) -> int:
        """Atomically adds one run to `day` and returns the new total (see JavaPlaygroundUsagePersistence)."""
        stmt = (
            pg_insert(NodePlaygroundDailyUsage)
            .values(day=day, runs=1)
            .on_conflict_do_update(
                index_elements=[NodePlaygroundDailyUsage.day],
                set_={"runs": NodePlaygroundDailyUsage.runs + 1},
            )
            .returning(NodePlaygroundDailyUsage.runs)
        )
        result = await self._execute(stmt)
        runs = result.scalar_one()
        await self._commit()
        return runs

    async def decrement(self, day: date) -> None:
        """Gives back one run for `day`, for a run the playground turned away (never below 0)."""
        stmt = (
            update(NodePlaygroundDailyUsage)
            .where(NodePlaygroundDailyUsage.day == day, NodePlaygroundDailyUsage.runs > 0)
            .values(runs=NodePlaygroundDailyUsage.runs - 1)
        )
        await self._execute(stmt)
        await self._commit()
