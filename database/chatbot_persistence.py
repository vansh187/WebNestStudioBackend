import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database.base_persistence import BasePersistence
from database.models import ChatbotMessage, ChatbotThread


class ChatbotPersistence(BasePersistence):
    """CRUD access to the chatbot_threads / chatbot_messages tables (the
    project-enquiry conversation's own internal turn log, used to build LLM
    context - distinct from the unified ChatMessage transcript the widget reads)."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def create_thread(
        self,
        user_id: uuid.UUID,
        contact_name: str | None,
        contact_email: str | None,
        contact_phone: str | None,
    ) -> ChatbotThread:
        collected = {
            "contact_name": contact_name,
            "contact_email": contact_email,
            "contact_phone": contact_phone,
        }
        thread = ChatbotThread(user_id=user_id, collected_fields_json=json.dumps(collected))
        self._session.add(thread)
        await self._commit()
        await self._refresh(thread)
        return thread

    async def append_message(self, thread_id: uuid.UUID, role: str, content: str) -> None:
        self._session.add(
            ChatbotMessage(thread_id=thread_id, role=role, content=content, created_at=datetime.now(timezone.utc))
        )
        await self._commit()

    async def get_by_id(self, thread_id: uuid.UUID) -> ChatbotThread | None:
        result = await self._execute(select(ChatbotThread).where(ChatbotThread.id == thread_id))
        return result.scalar_one_or_none()

    async def get_with_messages(self, thread_id: uuid.UUID) -> ChatbotThread | None:
        result = await self._execute(
            select(ChatbotThread).where(ChatbotThread.id == thread_id).options(selectinload(ChatbotThread.messages))
        )
        return result.scalar_one_or_none()

    async def update_state(
        self,
        thread: ChatbotThread,
        collected_fields_json: str,
        project_type: str | None,
        project_goal: str | None,
        pages_features: str | None,
        budget_range: str | None,
        timeline_expectation: str | None,
        status: str,
    ) -> ChatbotThread:
        thread.collected_fields_json = collected_fields_json
        thread.project_type = project_type
        thread.project_goal = project_goal
        thread.pages_features = pages_features
        thread.budget_range = budget_range
        thread.timeline_expectation = timeline_expectation
        thread.status = status
        await self._commit()
        await self._refresh(thread)
        return thread

    async def link_lead(self, thread: ChatbotThread, lead_id: uuid.UUID) -> ChatbotThread:
        thread.lead_id = lead_id
        await self._commit()
        await self._refresh(thread)
        return thread

    async def list_by_user(self, user_id: uuid.UUID) -> list[tuple[ChatbotThread, int]]:
        result = await self._execute(
            select(ChatbotThread, func.count(ChatbotMessage.id))
            .outerjoin(ChatbotMessage, ChatbotMessage.thread_id == ChatbotThread.id)
            .where(ChatbotThread.user_id == user_id)
            .group_by(ChatbotThread.id)
            .order_by(ChatbotThread.updated_at.desc())
        )
        return [(row[0], row[1]) for row in result.all()]
