import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.exceptions import DatabaseError
from database.base_persistence import BasePersistence
from database.models import Generation, GenerationMessage

logger = logging.getLogger("webnest.database")


class GenerationPersistence(BasePersistence):
    """CRUD access to the generations / generation_messages tables."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def create_thread(
        self, user_id: uuid.UUID, initial_prompt: str, html: str, title: str, provider_used: str
    ) -> Generation:
        generation = Generation(user_id=user_id, initial_prompt=initial_prompt, latest_html=html, title=title)
        self._session.add(generation)
        await self._flush_to_assign_id()

        # created_at is set explicitly in Python (rather than left to the DB's
        # func.now() default) because both rows below commit in the same
        # Postgres transaction, where NOW() is fixed for the whole transaction -
        # relying on it would give the user/assistant rows identical timestamps
        # and make GenerationMessage's created_at ordering non-deterministic.
        self._session.add(
            GenerationMessage(
                generation_id=generation.id,
                role="user",
                content=initial_prompt,
                created_at=datetime.now(timezone.utc),
            )
        )
        self._session.add(
            GenerationMessage(
                generation_id=generation.id,
                role="assistant",
                content="Generated page",
                html_snapshot=html,
                provider_used=provider_used,
                created_at=datetime.now(timezone.utc),
            )
        )
        await self._commit()
        await self._refresh(generation)
        return generation

    async def append_refinement(
        self, generation: Generation, refinement_text: str, html: str, provider_used: str
    ) -> Generation:
        self._session.add(
            GenerationMessage(
                generation_id=generation.id,
                role="user",
                content=refinement_text,
                created_at=datetime.now(timezone.utc),
            )
        )
        self._session.add(
            GenerationMessage(
                generation_id=generation.id,
                role="assistant",
                content=self._summarize_refinement(refinement_text),
                html_snapshot=html,
                provider_used=provider_used,
                created_at=datetime.now(timezone.utc),
            )
        )
        generation.latest_html = html
        await self._commit()
        await self._refresh(generation)
        return generation

    @staticmethod
    def _summarize_refinement(refinement_text: str) -> str:
        text = refinement_text.strip()
        return text if len(text) <= 80 else f"{text[:77]}..."

    async def get_by_id(self, generation_id: uuid.UUID) -> Generation | None:
        result = await self._execute(select(Generation).where(Generation.id == generation_id))
        return result.scalar_one_or_none()

    async def get_with_messages(self, generation_id: uuid.UUID) -> Generation | None:
        result = await self._execute(
            select(Generation).where(Generation.id == generation_id).options(selectinload(Generation.messages))
        )
        return result.scalar_one_or_none()

    async def list_by_user(self, user_id: uuid.UUID) -> list[tuple[Generation, int]]:
        result = await self._execute(
            select(Generation, func.count(GenerationMessage.id))
            .outerjoin(GenerationMessage, GenerationMessage.generation_id == Generation.id)
            .where(Generation.user_id == user_id)
            .group_by(Generation.id)
            .order_by(Generation.updated_at.desc())
        )
        return [(row[0], row[1]) for row in result.all()]

    async def _flush_to_assign_id(self) -> None:
        """Flushes so the DB-generated Generation.id is populated before dependent
        GenerationMessage rows are added in the same transaction (they need the FK)."""
        try:
            await self._session.flush()
        except SQLAlchemyError as exc:
            await self._session.rollback()
            logger.error("Database flush failed", exc_info=True)
            raise DatabaseError("Could not save data to the database right now. Please try again shortly.") from exc
