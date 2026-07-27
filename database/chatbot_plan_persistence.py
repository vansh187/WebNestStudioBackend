import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.base_persistence import BasePersistence
from database.models import ChatbotPlan


class ChatbotPlanPersistence(BasePersistence):
    """CRUD access to the chatbot_plans table. Append-only: regenerating a
    plan on the same thread inserts a new row, get_latest_by_thread serves
    the newest one."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def create(
        self,
        thread_id: uuid.UUID,
        user_id: uuid.UUID,
        project_type: str,
        total_weeks: int,
        plan_weeks_json: str,
        provider_used: str | None,
    ) -> ChatbotPlan:
        plan = ChatbotPlan(
            thread_id=thread_id,
            user_id=user_id,
            project_type=project_type,
            total_weeks=total_weeks,
            plan_weeks_json=plan_weeks_json,
            provider_used=provider_used,
        )
        self._session.add(plan)
        await self._commit()
        await self._refresh(plan)
        return plan

    async def get_latest_by_thread(self, thread_id: uuid.UUID) -> ChatbotPlan | None:
        result = await self._execute(
            select(ChatbotPlan).where(ChatbotPlan.thread_id == thread_id).order_by(ChatbotPlan.created_at.desc()).limit(1)
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, plan_id: uuid.UUID) -> ChatbotPlan | None:
        result = await self._execute(select(ChatbotPlan).where(ChatbotPlan.id == plan_id))
        return result.scalar_one_or_none()
