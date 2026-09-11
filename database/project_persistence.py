import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from core.constants import SDLC_STAGES
from database.base_persistence import BasePersistence
from database.models import Project, ProjectStage, User


class ProjectPersistence(BasePersistence):
    async def create(
        self,
        *,
        client_user_id: uuid.UUID,
        name: str,
        summary: str | None,
        current_stage: str,
        status: str,
        created_by: uuid.UUID | None,
        stage_rows: Sequence[dict],
    ) -> Project:
        project = Project(
            client_user_id=client_user_id,
            name=name,
            summary=summary,
            current_stage=current_stage,
            status=status,
            created_by=created_by,
        )
        self._session.add(project)
        await self._session.flush()

        for row in stage_rows:
            self._session.add(ProjectStage(project_id=project.id, **row))
        await self._commit("A project with these details already exists")
        return await self.get_by_id(project.id)  # type: ignore[return-value]

    async def list_for_client(self, client_user_id: uuid.UUID) -> list[Project]:
        result = await self._execute(
            select(Project)
            .where(Project.client_user_id == client_user_id)
            .order_by(Project.updated_at.desc())
            .options(selectinload(Project.stages))
        )
        return list(result.scalars().all())

    async def get_for_client(self, client_user_id: uuid.UUID, project_id: uuid.UUID) -> Project | None:
        result = await self._execute(
            select(Project)
            .where(Project.id == project_id, Project.client_user_id == client_user_id)
            .options(selectinload(Project.stages))
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, project_id: uuid.UUID) -> Project | None:
        result = await self._execute(
            select(Project).where(Project.id == project_id).options(selectinload(Project.stages))
        )
        return result.scalar_one_or_none()

    async def list_admin(
        self,
        *,
        client_email: str | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[tuple[Project, User]], int]:
        base = select(Project, User).join(User, User.id == Project.client_user_id)
        if client_email:
            base = base.where(func.lower(User.email) == client_email.strip().lower())
        if status:
            base = base.where(Project.status == status)

        count_result = await self._execute(select(func.count()).select_from(base.subquery()))
        total = count_result.scalar_one()

        rows_result = await self._execute(
            base.order_by(Project.updated_at.desc())
            .limit(limit)
            .offset(offset)
            .options(selectinload(Project.stages))
        )
        rows = [(project, user) for project, user in rows_result.all()]
        return rows, total

    async def update_project(self, project: Project, **fields) -> Project:
        for key, value in fields.items():
            setattr(project, key, value)
        await self._commit()
        await self._refresh(project)
        return project

    async def upsert_stage(self, project_id: uuid.UUID, key: str, **fields) -> ProjectStage | None:
        result = await self._execute(
            select(ProjectStage).where(ProjectStage.project_id == project_id, ProjectStage.key == key)
        )
        stage = result.scalar_one_or_none()
        if stage is None:
            return None
        for field_name, value in fields.items():
            setattr(stage, field_name, value)
        await self._commit()
        await self._refresh(stage)
        return stage

    async def archive(self, project: Project) -> None:
        project.status = "archived"
        await self._commit()

    async def hard_delete(self, project: Project) -> None:
        await self._delete(project)

    @staticmethod
    def seed_stage_rows(current_stage: str) -> list[dict]:
        """Six rows for a brand-new project: stages before current_stage are
        done, current_stage is in_progress, the rest pending."""
        rows: list[dict] = []
        reached = True
        for index, (key, label) in enumerate(SDLC_STAGES):
            if key == current_stage:
                rows.append(
                    {
                        "key": key,
                        "label": label,
                        "order_index": index,
                        "state": "in_progress",
                        "started_at": func.now(),
                    }
                )
                reached = False
            elif reached:
                rows.append(
                    {
                        "key": key,
                        "label": label,
                        "order_index": index,
                        "state": "done",
                        "started_at": func.now(),
                        "completed_at": func.now(),
                    }
                )
            else:
                rows.append({"key": key, "label": label, "order_index": index, "state": "pending"})
        return rows
