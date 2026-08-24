import uuid
from datetime import datetime

from sqlalchemy import case, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from database.base_persistence import BasePersistence
from database.models import ChatbotLimit


class ChatbotLimitPersistence(BasePersistence):
    """CRUD access to the chatbot_limits table (one row per user) - its own
    hourly-window bucket, separate from the AI Page Builder's generation_limits."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def get_by_user(self, user_id: uuid.UUID) -> ChatbotLimit | None:
        result = await self._execute(select(ChatbotLimit).where(ChatbotLimit.user_id == user_id))
        return result.scalar_one_or_none()

    async def try_consume(
        self, user_id: uuid.UUID, limit_per_hour: int, now: datetime, window_cutoff: datetime
    ) -> bool:
        """Same atomic INSERT ... ON CONFLICT DO UPDATE ... WHERE technique as
        GenerationLimitPersistence.try_consume - see that method's docstring
        for why this must be a single statement rather than SELECT-then-UPDATE."""
        reset_expr = ChatbotLimit.window_start < window_cutoff
        stmt = (
            pg_insert(ChatbotLimit)
            .values(user_id=user_id, count=1, window_start=now)
            .on_conflict_do_update(
                index_elements=[ChatbotLimit.user_id],
                set_={
                    "count": case((reset_expr, 1), else_=ChatbotLimit.count + 1),
                    "window_start": case((reset_expr, now), else_=ChatbotLimit.window_start),
                },
                where=or_(reset_expr, ChatbotLimit.count < limit_per_hour),
            )
            .returning(ChatbotLimit.id)
        )
        result = await self._execute(stmt)
        await self._commit(conflict_message="Rate limit record already exists for this user")
        return result.first() is not None
