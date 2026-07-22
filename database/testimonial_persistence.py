import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.base_persistence import BasePersistence
from database.models import Testimonial


class TestimonialPersistence(BasePersistence):
    """CRUD access to the testimonials table."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def create(self, **fields) -> Testimonial:
        testimonial = Testimonial(**fields)
        self._session.add(testimonial)
        await self._commit(conflict_message="This testimonial could not be saved due to conflicting data")
        await self._refresh(testimonial)
        return testimonial

    async def get_by_id(self, testimonial_id: uuid.UUID) -> Testimonial | None:
        result = await self._execute(select(Testimonial).where(Testimonial.id == testimonial_id))
        return result.scalar_one_or_none()

    async def list_published(self, limit: int | None = None) -> list[Testimonial]:
        query = select(Testimonial).where(Testimonial.is_published.is_(True)).order_by(Testimonial.created_at.desc())
        if limit:
            query = query.limit(limit)
        result = await self._execute(query)
        return list(result.scalars().all())

    async def list_all(self) -> list[Testimonial]:
        result = await self._execute(select(Testimonial).order_by(Testimonial.created_at.desc()))
        return list(result.scalars().all())

    async def update(self, testimonial: Testimonial, **fields) -> Testimonial:
        for key, value in fields.items():
            if value is not None:
                setattr(testimonial, key, value)
        await self._commit(conflict_message="This testimonial could not be saved due to conflicting data")
        await self._refresh(testimonial)
        return testimonial

    async def delete(self, testimonial: Testimonial) -> None:
        await self._delete(testimonial)
