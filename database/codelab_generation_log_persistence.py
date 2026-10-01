from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.base_persistence import BasePersistence
from database.models import CodelabGenerationLog


class CodelabGenerationLogPersistence(BasePersistence):
    """Write/read access to the codelab_generation_logs audit trail."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def create(self, **fields) -> CodelabGenerationLog:
        log = CodelabGenerationLog(**fields)
        self._session.add(log)
        await self._commit(conflict_message="Could not record this generation attempt")
        await self._refresh(log)
        return log

    async def list_recent(self, limit: int = 30) -> list[CodelabGenerationLog]:
        query = select(CodelabGenerationLog).order_by(CodelabGenerationLog.attempted_at.desc()).limit(limit)
        result = await self._execute(query)
        return list(result.scalars().all())

    async def latest_success_at(self, difficulty: str) -> datetime | None:
        """When a problem of this difficulty was last generated successfully."""
        query = select(func.max(CodelabGenerationLog.attempted_at)).where(
            CodelabGenerationLog.success.is_(True), CodelabGenerationLog.difficulty == difficulty
        )
        result = await self._execute(query)
        return result.scalar_one_or_none()

    async def attempt_counts_by_topic(self, difficulty: str) -> dict[str, int]:
        """How many runs (successful or not) each curriculum topic has had for
        this difficulty. Counting failures too is what lets the rotation move
        on from a topic that keeps failing verification."""
        query = (
            select(CodelabGenerationLog.topic, func.count(CodelabGenerationLog.id))
            .where(CodelabGenerationLog.difficulty == difficulty, CodelabGenerationLog.topic.is_not(None))
            .group_by(CodelabGenerationLog.topic)
        )
        result = await self._execute(query)
        return {row[0]: row[1] for row in result.all()}
