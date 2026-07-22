import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import NotFoundError
from database.models import PortfolioItem
from database.portfolio_persistence import PortfolioPersistence


class PortfolioService:
    """Business logic for portfolio/work items."""

    def __init__(self, session: AsyncSession) -> None:
        self._portfolio = PortfolioPersistence(session)

    async def list_published(self, category: str | None = None, limit: int | None = None) -> list[PortfolioItem]:
        return await self._portfolio.list_published(category=category, limit=limit)

    async def list_all(self) -> list[PortfolioItem]:
        return await self._portfolio.list_all()

    async def get_by_slug(self, slug: str) -> PortfolioItem:
        item = await self._portfolio.get_by_slug(slug)
        if item is None:
            raise NotFoundError("Portfolio item not found")
        return item

    async def create(self, **fields) -> PortfolioItem:
        return await self._portfolio.create(**fields)

    async def update(self, item_id: uuid.UUID, **fields) -> PortfolioItem:
        item = await self._portfolio.get_by_id(item_id)
        if item is None:
            raise NotFoundError("Portfolio item not found")
        return await self._portfolio.update(item, **fields)

    async def delete(self, item_id: uuid.UUID) -> None:
        item = await self._portfolio.get_by_id(item_id)
        if item is None:
            raise NotFoundError("Portfolio item not found")
        await self._portfolio.delete(item)
