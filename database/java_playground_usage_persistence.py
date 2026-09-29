from datetime import date

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from database.base_persistence import BasePersistence
from database.models import JavaPlaygroundDailyUsage


class JavaPlaygroundUsagePersistence(BasePersistence):
    """Access to the java_playground_daily_usage table (one row per UTC day)."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def increment(self, day: date) -> int:
        """Atomically adds one run to `day` and returns the new total.

        A single INSERT ... ON CONFLICT DO UPDATE ... RETURNING so concurrent
        requests can never read the same pre-increment count.
        """
        stmt = (
            pg_insert(JavaPlaygroundDailyUsage)
            .values(day=day, runs=1)
            .on_conflict_do_update(
                index_elements=[JavaPlaygroundDailyUsage.day],
                set_={"runs": JavaPlaygroundDailyUsage.runs + 1},
            )
            .returning(JavaPlaygroundDailyUsage.runs)
        )
        result = await self._execute(stmt)
        runs = result.scalar_one()
        await self._commit()
        return runs
