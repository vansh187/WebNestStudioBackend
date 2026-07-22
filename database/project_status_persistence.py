import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.base_persistence import BasePersistence
from database.models import ProjectStatus


class ProjectStatusPersistence(BasePersistence):
    """CRUD access to the project_status table."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def get_by_client(self, client_user_id: uuid.UUID) -> ProjectStatus | None:
        result = await self._execute(select(ProjectStatus).where(ProjectStatus.client_user_id == client_user_id))
        return result.scalars().first()

    async def upsert(
        self,
        client_user_id: uuid.UUID,
        project_name: str | None,
        phase: str | None,
        percent_complete: int | None,
    ) -> ProjectStatus:
        existing = await self.get_by_client(client_user_id)
        if existing is None:
            existing = ProjectStatus(
                client_user_id=client_user_id,
                project_name=project_name,
                phase=phase,
                percent_complete=percent_complete,
            )
            self._session.add(existing)
        else:
            if project_name is not None:
                existing.project_name = project_name
            if phase is not None:
                existing.phase = phase
            if percent_complete is not None:
                existing.percent_complete = percent_complete
        await self._commit(conflict_message="This project status could not be saved due to conflicting data")
        await self._refresh(existing)
        return existing
