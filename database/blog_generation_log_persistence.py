from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.base_persistence import BasePersistence
from database.models import BlogGenerationLog


class BlogGenerationLogPersistence(BasePersistence):
    """Write/read access to the blog_generation_logs audit trail."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def create(self, **fields) -> BlogGenerationLog:
        log = BlogGenerationLog(**fields)
        self._session.add(log)
        await self._commit(conflict_message="Could not record this generation attempt")
        await self._refresh(log)
        return log

    async def list_recent(self, limit: int = 20) -> list[BlogGenerationLog]:
        query = select(BlogGenerationLog).order_by(BlogGenerationLog.attempted_at.desc()).limit(limit)
        result = await self._execute(query)
        return list(result.scalars().all())

    async def has_successful_run(self, trigger_source: str) -> bool:
        """Whether a run with this trigger_source has ever succeeded, ever -
        not scoped to today. Used to make the one-off launch post a true
        one-time event: relying on "published today" alone would make it
        re-fire on every restart/redeploy that happens after the launch date
        on any day nothing else has published yet."""
        query = select(BlogGenerationLog.id).where(
            BlogGenerationLog.trigger_source == trigger_source,
            BlogGenerationLog.success.is_(True),
        ).limit(1)
        result = await self._execute(query)
        return result.scalar_one_or_none() is not None
