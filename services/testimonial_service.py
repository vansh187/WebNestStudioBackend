import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import NotFoundError
from database.models import Testimonial
from database.testimonial_persistence import TestimonialPersistence


class TestimonialService:
    """Business logic for client testimonials."""

    def __init__(self, session: AsyncSession) -> None:
        self._testimonials = TestimonialPersistence(session)

    async def list_published(self, limit: int | None = None) -> list[Testimonial]:
        return await self._testimonials.list_published(limit=limit)

    async def list_all(self) -> list[Testimonial]:
        return await self._testimonials.list_all()

    async def create(self, **fields) -> Testimonial:
        return await self._testimonials.create(**fields)

    async def update(self, testimonial_id: uuid.UUID, **fields) -> Testimonial:
        testimonial = await self._testimonials.get_by_id(testimonial_id)
        if testimonial is None:
            raise NotFoundError("Testimonial not found")
        return await self._testimonials.update(testimonial, **fields)

    async def delete(self, testimonial_id: uuid.UUID) -> None:
        testimonial = await self._testimonials.get_by_id(testimonial_id)
        if testimonial is None:
            raise NotFoundError("Testimonial not found")
        await self._testimonials.delete(testimonial)
