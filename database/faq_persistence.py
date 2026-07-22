import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.base_persistence import BasePersistence
from database.models import Faq


class FaqPersistence(BasePersistence):
    """CRUD access to the faqs table."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def create(self, **fields) -> Faq:
        faq = Faq(**fields)
        self._session.add(faq)
        await self._commit(conflict_message="This FAQ could not be saved due to conflicting data")
        await self._refresh(faq)
        return faq

    async def get_by_id(self, faq_id: uuid.UUID) -> Faq | None:
        result = await self._execute(select(Faq).where(Faq.id == faq_id))
        return result.scalar_one_or_none()

    async def list_published(self, category: str | None = None) -> list[Faq]:
        query = select(Faq).where(Faq.is_published.is_(True))
        if category:
            query = query.where(Faq.category == category)
        query = query.order_by(Faq.display_order)
        result = await self._execute(query)
        return list(result.scalars().all())

    async def list_all(self) -> list[Faq]:
        result = await self._execute(select(Faq).order_by(Faq.display_order))
        return list(result.scalars().all())

    async def update(self, faq: Faq, **fields) -> Faq:
        for key, value in fields.items():
            if value is not None:
                setattr(faq, key, value)
        await self._commit(conflict_message="This FAQ could not be saved due to conflicting data")
        await self._refresh(faq)
        return faq

    async def delete(self, faq: Faq) -> None:
        await self._delete(faq)
