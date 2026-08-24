import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.base_persistence import BasePersistence
from database.models import PortfolioItem


class PortfolioPersistence(BasePersistence):
    """CRUD access to the portfolio_items table."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def create(self, **fields) -> PortfolioItem:
        item = PortfolioItem(**fields)
        self._session.add(item)
        await self._commit(conflict_message="A portfolio item with this slug already exists")
        await self._refresh(item)
        return item

    async def get_by_id(self, item_id: uuid.UUID) -> PortfolioItem | None:
        result = await self._execute(select(PortfolioItem).where(PortfolioItem.id == item_id))
        return result.scalar_one_or_none()

    async def get_by_slug(self, slug: str) -> PortfolioItem | None:
        result = await self._execute(select(PortfolioItem).where(PortfolioItem.slug == slug))
        return result.scalar_one_or_none()

    async def list_published(self, category: str | None = None, limit: int | None = None) -> list[PortfolioItem]:
        query = select(PortfolioItem).where(PortfolioItem.is_published.is_(True))
        if category:
            query = query.where(PortfolioItem.category == category)
        query = query.order_by(PortfolioItem.display_order)
        if limit:
            query = query.limit(limit)
        result = await self._execute(query)
        return list(result.scalars().all())

    async def list_all(self) -> list[PortfolioItem]:
        result = await self._execute(select(PortfolioItem).order_by(PortfolioItem.display_order))
        return list(result.scalars().all())

    async def update(self, item: PortfolioItem, **fields) -> PortfolioItem:
        for key, value in fields.items():
            if value is not None:
                setattr(item, key, value)
        await self._commit(conflict_message="A portfolio item with this slug already exists")
        await self._refresh(item)
        return item

    async def delete(self, item: PortfolioItem) -> None:
        await self._delete(item)
