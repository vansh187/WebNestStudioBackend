import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import NotFoundError
from database.lead_persistence import LeadPersistence
from database.models import Lead


class LeadService:
    """Handles the single leads pipeline shared by every form entry point.

    Does not send the team notification email itself: that's a slow,
    best-effort network call that must never block the request/response
    cycle, so callers are expected to schedule it (e.g. via FastAPI
    BackgroundTasks) using the Lead this returns.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._leads = LeadPersistence(session)

    async def create_lead(self, ip_address: str | None, user_agent: str | None, **fields) -> Lead:
        return await self._leads.create(ip_address=ip_address, user_agent=user_agent, **fields)

    async def list_leads(self, source: str | None, status: str | None, limit: int, offset: int) -> tuple[list[Lead], int]:
        leads = await self._leads.list_leads(source=source, status=status, limit=limit, offset=offset)
        total = await self._leads.count_leads(source=source, status=status)
        return leads, total

    async def update_lead_status(self, lead_id: uuid.UUID, status: str) -> Lead:
        lead = await self._leads.get_by_id(lead_id)
        if lead is None:
            raise NotFoundError("Lead not found")
        return await self._leads.update_status(lead, status)

    async def delete_lead(self, lead_id: uuid.UUID) -> None:
        lead = await self._leads.get_by_id(lead_id)
        if lead is None:
            raise NotFoundError("Lead not found")
        await self._leads.soft_delete(lead)
