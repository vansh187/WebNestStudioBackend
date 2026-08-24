import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import NotFoundError
from database.faq_persistence import FaqPersistence
from database.models import Faq


class FaqService:
    """Business logic for FAQs."""

    def __init__(self, session: AsyncSession) -> None:
        self._faqs = FaqPersistence(session)

    async def list_published(self, category: str | None = None) -> list[Faq]:
        return await self._faqs.list_published(category=category)

    async def list_all(self) -> list[Faq]:
        return await self._faqs.list_all()

    async def create(self, **fields) -> Faq:
        return await self._faqs.create(**fields)

    async def update(self, faq_id: uuid.UUID, **fields) -> Faq:
        faq = await self._faqs.get_by_id(faq_id)
        if faq is None:
            raise NotFoundError("FAQ not found")
        return await self._faqs.update(faq, **fields)

    async def delete(self, faq_id: uuid.UUID) -> None:
        faq = await self._faqs.get_by_id(faq_id)
        if faq is None:
            raise NotFoundError("FAQ not found")
        await self._faqs.delete(faq)
