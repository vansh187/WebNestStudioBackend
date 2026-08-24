import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.base_persistence import BasePersistence
from database.models import Lead


class LeadPersistence(BasePersistence):
    """CRUD access to the leads table (single table backing every form entry point)."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def create(self, **fields) -> Lead:
        lead = Lead(**fields)
        self._session.add(lead)
        await self._commit(conflict_message="This lead could not be saved due to conflicting data")
        await self._refresh(lead)
        return lead

    async def get_by_id(self, lead_id: uuid.UUID) -> Lead | None:
        result = await self._execute(select(Lead).where(Lead.id == lead_id, Lead.is_deleted.is_(False)))
        return result.scalar_one_or_none()

    async def list_leads(
        self,
        source: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Lead]:
        query = select(Lead).where(Lead.is_deleted.is_(False))
        if source:
            query = query.where(Lead.source == source)
        if status:
            query = query.where(Lead.status == status)
        query = query.order_by(Lead.created_at.desc()).limit(limit).offset(offset)
        result = await self._execute(query)
        return list(result.scalars().all())

    async def count_leads(self, source: str | None = None, status: str | None = None) -> int:
        query = select(func.count()).select_from(Lead).where(Lead.is_deleted.is_(False))
        if source:
            query = query.where(Lead.source == source)
        if status:
            query = query.where(Lead.status == status)
        result = await self._execute(query)
        return int(result.scalar_one())

    async def update_status(self, lead: Lead, status: str) -> Lead:
        lead.status = status
        await self._commit()
        await self._refresh(lead)
        return lead

    async def update_fields(self, lead: Lead, **fields) -> Lead:
        for name, value in fields.items():
            setattr(lead, name, value)
        await self._commit()
        await self._refresh(lead)
        return lead

    async def soft_delete(self, lead: Lead) -> Lead:
        lead.is_deleted = True
        await self._commit()
        await self._refresh(lead)
        return lead
