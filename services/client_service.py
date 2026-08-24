import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import NotFoundError
from database.models import ProjectStatus
from database.project_status_persistence import ProjectStatusPersistence


class ClientService:
    """Business logic for the client portal (project status, files)."""

    def __init__(self, session: AsyncSession) -> None:
        self._project_status = ProjectStatusPersistence(session)

    async def get_project_status(self, client_user_id: uuid.UUID) -> ProjectStatus:
        status = await self._project_status.get_by_client(client_user_id)
        if status is None:
            raise NotFoundError("No project status found for this client yet")
        return status

    async def set_project_status(
        self,
        client_user_id: uuid.UUID,
        project_name: str | None,
        phase: str | None,
        percent_complete: int | None,
    ) -> ProjectStatus:
        return await self._project_status.upsert(client_user_id, project_name, phase, percent_complete)
