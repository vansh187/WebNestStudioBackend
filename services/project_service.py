import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from core.constants import SDLC_STAGE_KEYS, SDLC_STAGE_LABELS
from core.exceptions import NotFoundError, ValidationError
from database.messaging_persistence import MessagingPersistence
from database.models import Project, User
from database.project_persistence import ProjectPersistence
from database.user_persistence import UserPersistence
from schemas.client_schemas import ProjectStatusResponse
from schemas.project_schemas import (
    AdminProjectRow,
    ProjectDetailResponse,
    ProjectStageResponse,
    ProjectSummaryResponse,
)
from services.messaging_service import MessagingService

logger = logging.getLogger("webnest.projects")


def effective_progress(project: Project) -> int:
    """A resolved 0-100 integer: the admin's pinned value if set, else computed
    from stage completion (project-progress-backend-spec.md section 3)."""
    if project.progress_percent is not None:
        return project.progress_percent
    stages = project.stages or []
    if not stages:
        return 0
    done = sum(1 for s in stages if s.state == "done")
    doing = sum(1 for s in stages if s.state == "in_progress")
    return round(100 * (done + 0.5 * doing) / len(SDLC_STAGE_KEYS))


def _furthest_non_pending_stage(project: Project) -> str:
    """The furthest stage that isn't pending, or the first stage if every
    stage is still pending. Stages are always seeded in order_index 0->5."""
    ordered = sorted(project.stages or [], key=lambda s: s.order_index)
    furthest = None
    for stage in ordered:
        if stage.state != "pending":
            furthest = stage.key
    return furthest or (ordered[0].key if ordered else SDLC_STAGE_KEYS[0])


class ProjectService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._projects = ProjectPersistence(session)
        self._users = UserPersistence(session)
        # MessagingService.create_project_conversation is reused as-is — no
        # edit needed there (chat-backend-spec.md section 11).
        self._messaging_service = MessagingService(
            session=session,
            settings=None,  # unused by create_project_conversation
            storage_service=None,  # unused by create_project_conversation
        )
        self._messaging = MessagingPersistence(session)

    # ---------------------------------------------------------- client --- #

    async def list_for_client(self, client_user_id: uuid.UUID) -> list[ProjectSummaryResponse]:
        projects = await self._projects.list_for_client(client_user_id)
        conversation_ids = await self._conversation_ids_for(p.id for p in projects)
        return [self._to_summary(p, conversation_ids.get(p.id)) for p in projects]

    async def get_for_client(self, client_user_id: uuid.UUID, project_id: uuid.UUID) -> ProjectDetailResponse:
        project = await self._projects.get_for_client(client_user_id, project_id)
        if project is None:
            raise NotFoundError("Project not found")
        conversation_ids = await self._conversation_ids_for([project.id])
        return self._to_detail(project, conversation_ids.get(project.id))

    async def legacy_status_for_client(self, client_user_id: uuid.UUID) -> ProjectStatusResponse:
        projects = await self._projects.list_for_client(client_user_id)
        active = next((p for p in projects if p.status != "archived"), None)
        if active is None:
            raise NotFoundError("No project found")
        return ProjectStatusResponse(
            id=active.id,
            client_user_id=active.client_user_id,
            project_name=active.name,
            phase=SDLC_STAGE_LABELS.get(active.current_stage, active.current_stage),
            percent_complete=effective_progress(active),
            updated_at=active.updated_at,
        )

    # ----------------------------------------------------------- admin --- #

    async def list_admin(
        self, *, client_email: str | None, status: str | None, limit: int, offset: int
    ) -> tuple[list[AdminProjectRow], int]:
        rows, total = await self._projects.list_admin(
            client_email=client_email, status=status, limit=limit, offset=offset
        )
        conversation_ids = await self._conversation_ids_for(p.id for p, _ in rows)
        return [self._to_admin_row(p, u, conversation_ids.get(p.id)) for p, u in rows], total

    async def get_admin(
        self, project_id: uuid.UUID, *, admin_user_id: uuid.UUID | None = None
    ) -> AdminProjectRow:
        project = await self._projects.get_by_id(project_id)
        if project is None:
            raise NotFoundError("Project not found")
        user = await self._users.get_by_id(project.client_user_id)
        # Viewing a project's detail is also how an admin reaches its team
        # chat — self-heal membership here so "Open project team chat" never
        # 403s for whichever admin is looking, not just the one who created it.
        if admin_user_id is not None:
            conversation_id = await self._ensure_admin_in_conversation(project.id, admin_user_id)
        else:
            conversation_ids = await self._conversation_ids_for([project.id])
            conversation_id = conversation_ids.get(project.id)
        return self._to_admin_row(project, user, conversation_id)

    async def create_project(
        self,
        *,
        admin_user_id: uuid.UUID,
        client_email: str,
        name: str,
        summary: str | None,
        current_stage: str,
        create_conversation: bool,
    ) -> AdminProjectRow:
        client = await self._users.get_by_email_ci(client_email)
        if client is None:
            raise NotFoundError("No user with that email")
        if current_stage not in SDLC_STAGE_KEYS:
            raise ValidationError("current_stage is not a valid SDLC stage")

        stage_rows = ProjectPersistence.seed_stage_rows(current_stage)
        project = await self._projects.create(
            client_user_id=client.id,
            name=name.strip(),
            summary=(summary or "").strip() or None,
            current_stage=current_stage,
            status="active",
            created_by=admin_user_id,
            stage_rows=stage_rows,
        )

        conversation_id = None
        if create_conversation:
            # create_project_conversation commits on its own session and is
            # idempotent per project_id (chat-backend-spec.md section 11) — a
            # failure here leaves the project row intact; an admin can retry
            # via PATCH .../projects/{id} with create_conversation: true.
            try:
                conversation = await self._messaging_service.create_project_conversation(
                    owner_user_id=client.id, title=project.name, project_id=project.id
                )
                conversation_id = conversation.id
                await self._add_admin_to_conversation(conversation, admin_user_id)
            except Exception:
                logger.exception("Failed to create team chat for new project %s", project.id)

        return self._to_admin_row(project, client, conversation_id)

    async def update_project(
        self, project_id: uuid.UUID, *, admin_user_id: uuid.UUID | None = None, **fields
    ) -> AdminProjectRow:
        project = await self._projects.get_by_id(project_id)
        if project is None:
            raise NotFoundError("Project not found")

        create_conversation = fields.pop("create_conversation", None)
        current_stage = fields.pop("current_stage", None)
        if current_stage is not None:
            if current_stage not in SDLC_STAGE_KEYS:
                raise ValidationError("current_stage is not a valid SDLC stage")
            await self._cascade_stage(project, current_stage)
            fields["current_stage"] = current_stage

        write_fields = {k: v for k, v in fields.items() if v is not None or k == "progress_percent"}
        if write_fields:
            project = await self._projects.update_project(project, **write_fields)
        else:
            project = await self._projects.get_by_id(project_id)

        conversation_id = None
        if create_conversation:
            try:
                conversation = await self._messaging_service.create_project_conversation(
                    owner_user_id=project.client_user_id, title=project.name, project_id=project.id
                )
                conversation_id = conversation.id
                if admin_user_id is not None:
                    await self._add_admin_to_conversation(conversation, admin_user_id)
            except Exception:
                logger.exception("Failed to create team chat for project %s", project.id)
        elif admin_user_id is not None:
            conversation_id = await self._ensure_admin_in_conversation(project.id, admin_user_id)
        else:
            conversation_ids = await self._conversation_ids_for([project.id])
            conversation_id = conversation_ids.get(project.id)

        user = await self._users.get_by_id(project.client_user_id)
        return self._to_admin_row(project, user, conversation_id)

    async def update_stage(
        self, project_id: uuid.UUID, stage_key: str, *, state: str | None, note: str | None
    ) -> AdminProjectRow:
        if stage_key not in SDLC_STAGE_KEYS:
            raise NotFoundError("Unknown stage")
        project = await self._projects.get_by_id(project_id)
        if project is None:
            raise NotFoundError("Project not found")

        fields: dict = {}
        if state is not None:
            fields["state"] = state
            if state == "in_progress":
                fields["started_at"] = _now()
            elif state == "done":
                fields["completed_at"] = _now()
            else:
                fields["completed_at"] = None
        if note is not None:
            fields["note"] = note or None
        if fields:
            updated = await self._projects.upsert_stage(project_id, stage_key, **fields)
            if updated is None:
                raise NotFoundError("Unknown stage")

        project = await self._projects.get_by_id(project_id)
        furthest = _furthest_non_pending_stage(project)
        if furthest != project.current_stage:
            project = await self._projects.update_project(project, current_stage=furthest)

        user = await self._users.get_by_id(project.client_user_id)
        conversation_ids = await self._conversation_ids_for([project.id])
        return self._to_admin_row(project, user, conversation_ids.get(project.id))

    async def archive(self, project_id: uuid.UUID) -> None:
        project = await self._projects.get_by_id(project_id)
        if project is None:
            raise NotFoundError("Project not found")
        await self._projects.archive(project)

    async def hard_delete(self, project_id: uuid.UUID) -> None:
        project = await self._projects.get_by_id(project_id)
        if project is None:
            raise NotFoundError("Project not found")
        await self._projects.hard_delete(project)

    # ---------------------------------------------------------- helpers --- #

    async def _cascade_stage(self, project: Project, current_stage: str) -> None:
        """Overrides any manual per-stage edits: earlier -> done, this -> in_progress,
        later -> pending, with timestamps set/cleared on the transition."""
        target_index = SDLC_STAGE_KEYS.index(current_stage)
        for stage in project.stages:
            if stage.order_index < target_index:
                await self._projects.upsert_stage(
                    project.id, stage.key, state="done", completed_at=_now()
                )
            elif stage.order_index == target_index:
                await self._projects.upsert_stage(
                    project.id, stage.key, state="in_progress", started_at=_now(), completed_at=None
                )
            else:
                await self._projects.upsert_stage(
                    project.id, stage.key, state="pending", started_at=None, completed_at=None
                )

    async def _conversation_ids_for(self, project_ids) -> dict:
        ids = list(project_ids)
        if not ids:
            return {}
        result = {}
        for project_id in ids:
            conversation = await self._messaging.get_conversation_by_project(project_id)
            if conversation is not None:
                result[project_id] = conversation.id
        return result

    async def _add_admin_to_conversation(self, conversation, admin_user_id: uuid.UUID) -> None:
        # Idempotent — add_participants no-ops for an already-active member.
        # The client stays `owner`; the admin joins as a normal `member` (the
        # existing any-participant-can-add rule already lets them add more
        # WebNest staff or be added back if they ever leave).
        await self._messaging.add_participants(conversation, [admin_user_id])

    async def _ensure_admin_in_conversation(
        self, project_id: uuid.UUID, admin_user_id: uuid.UUID
    ) -> uuid.UUID | None:
        conversation = await self._messaging.get_conversation_by_project(project_id)
        if conversation is None:
            return None
        await self._add_admin_to_conversation(conversation, admin_user_id)
        return conversation.id

    def _to_stage(self, stage) -> ProjectStageResponse:
        return ProjectStageResponse.model_validate(stage)

    def _to_summary(self, project: Project, conversation_id: uuid.UUID | None) -> ProjectSummaryResponse:
        return ProjectSummaryResponse(
            id=project.id,
            name=project.name,
            status=project.status,
            current_stage=project.current_stage,
            current_stage_label=SDLC_STAGE_LABELS.get(project.current_stage, project.current_stage),
            progress_percent=effective_progress(project),
            conversation_id=conversation_id,
            updated_at=project.updated_at,
        )

    def _to_detail(self, project: Project, conversation_id: uuid.UUID | None) -> ProjectDetailResponse:
        summary = self._to_summary(project, conversation_id)
        stages = sorted(project.stages, key=lambda s: s.order_index)
        return ProjectDetailResponse(
            **summary.model_dump(),
            summary=project.summary,
            created_at=project.created_at,
            stages=[self._to_stage(s) for s in stages],
        )

    def _to_admin_row(self, project: Project, user: User | None, conversation_id: uuid.UUID | None) -> AdminProjectRow:
        detail = self._to_detail(project, conversation_id)
        return AdminProjectRow(
            **detail.model_dump(),
            client_email=user.email if user else "",
            client_name=user.full_name if user else None,
        )


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
