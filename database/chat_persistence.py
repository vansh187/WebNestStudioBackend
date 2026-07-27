import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database.base_persistence import BasePersistence
from database.models import ChatMessage, ChatThread


class ChatPersistence(BasePersistence):
    """CRUD access to chat_threads / chat_messages - the unified transcript
    the widget reads, regardless of which underlying feature (page builder
    or enquiry) is handling the thread."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def create_thread(self, user_id: uuid.UUID) -> ChatThread:
        thread = ChatThread(user_id=user_id)
        self._session.add(thread)
        await self._commit()
        await self._refresh(thread)
        return thread

    async def append_message(self, chat_thread_id: uuid.UUID, role: str, content: str, mode: str) -> None:
        self._session.add(
            ChatMessage(
                chat_thread_id=chat_thread_id,
                role=role,
                content=content,
                mode=mode,
                created_at=datetime.now(timezone.utc),
            )
        )
        await self._commit()

    async def get_by_id(self, thread_id: uuid.UUID) -> ChatThread | None:
        result = await self._execute(select(ChatThread).where(ChatThread.id == thread_id))
        return result.scalar_one_or_none()

    async def get_with_messages(self, thread_id: uuid.UUID) -> ChatThread | None:
        result = await self._execute(
            select(ChatThread).where(ChatThread.id == thread_id).options(selectinload(ChatThread.messages))
        )
        return result.scalar_one_or_none()

    async def lock_mode(
        self,
        thread: ChatThread,
        mode: str,
        generation_id: uuid.UUID | None = None,
        enquiry_id: uuid.UUID | None = None,
    ) -> ChatThread:
        thread.mode = mode
        if generation_id is not None:
            thread.generation_id = generation_id
        if enquiry_id is not None:
            thread.enquiry_id = enquiry_id
        await self._commit()
        await self._refresh(thread)
        return thread

    async def list_by_user(self, user_id: uuid.UUID) -> list[tuple[ChatThread, int]]:
        result = await self._execute(
            select(ChatThread, func.count(ChatMessage.id))
            .outerjoin(ChatMessage, ChatMessage.chat_thread_id == ChatThread.id)
            .where(ChatThread.user_id == user_id)
            .group_by(ChatThread.id)
            .order_by(ChatThread.updated_at.desc())
        )
        return [(row[0], row[1]) for row in result.all()]
