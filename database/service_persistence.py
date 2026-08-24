import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.base_persistence import BasePersistence
from database.models import Service


class ServicePersistence(BasePersistence):
    """CRUD access to the services table."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def create(self, **fields) -> Service:
        service = Service(**fields)
        self._session.add(service)
        await self._commit(conflict_message="A service with this slug already exists")
        await self._refresh(service)
        return service

    async def get_by_id(self, service_id: uuid.UUID) -> Service | None:
        result = await self._execute(select(Service).where(Service.id == service_id))
        return result.scalar_one_or_none()

    async def get_by_slug(self, slug: str) -> Service | None:
        result = await self._execute(select(Service).where(Service.slug == slug))
        return result.scalar_one_or_none()

    async def list_published(self, limit: int | None = None) -> list[Service]:
        query = select(Service).where(Service.is_published.is_(True)).order_by(Service.display_order)
        if limit:
            query = query.limit(limit)
        result = await self._execute(query)
        return list(result.scalars().all())

    async def list_all(self) -> list[Service]:
        result = await self._execute(select(Service).order_by(Service.display_order))
        return list(result.scalars().all())

    async def update(self, service: Service, **fields) -> Service:
        for key, value in fields.items():
            if value is not None:
                setattr(service, key, value)
        await self._commit(conflict_message="A service with this slug already exists")
        await self._refresh(service)
        return service

    async def delete(self, service: Service) -> None:
        await self._delete(service)
