import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import NotFoundError
from database.models import Service
from database.service_persistence import ServicePersistence


class ServiceCatalogService:
    """Business logic for the public 'services' offering catalog."""

    def __init__(self, session: AsyncSession) -> None:
        self._services = ServicePersistence(session)

    async def list_published(self, limit: int | None = None) -> list[Service]:
        return await self._services.list_published(limit=limit)

    async def list_all(self) -> list[Service]:
        return await self._services.list_all()

    async def get_by_slug(self, slug: str) -> Service:
        service = await self._services.get_by_slug(slug)
        if service is None:
            raise NotFoundError("Service not found")
        return service

    async def create(self, **fields) -> Service:
        return await self._services.create(**fields)

    async def update(self, service_id: uuid.UUID, **fields) -> Service:
        service = await self._services.get_by_id(service_id)
        if service is None:
            raise NotFoundError("Service not found")
        return await self._services.update(service, **fields)

    async def delete(self, service_id: uuid.UUID) -> None:
        service = await self._services.get_by_id(service_id)
        if service is None:
            raise NotFoundError("Service not found")
        await self._services.delete(service)
