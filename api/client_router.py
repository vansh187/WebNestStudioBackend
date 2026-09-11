import uuid

from fastapi import APIRouter, Depends

from core.dependencies import get_project_service, require_client
from database.models import User
from schemas.client_schemas import ProjectStatusResponse
from schemas.project_schemas import MyProjectListResponse, ProjectDetailResponse
from services.project_service import ProjectService

router = APIRouter(prefix="/api/me", tags=["client"])


@router.get("/projects", response_model=MyProjectListResponse)
async def list_my_projects(
    current_user: User = Depends(require_client),
    project_service: ProjectService = Depends(get_project_service),
) -> MyProjectListResponse:
    projects = await project_service.list_for_client(current_user.id)
    return MyProjectListResponse(projects=projects)


@router.get("/projects/{project_id}", response_model=ProjectDetailResponse)
async def get_my_project(
    project_id: uuid.UUID,
    current_user: User = Depends(require_client),
    project_service: ProjectService = Depends(get_project_service),
) -> ProjectDetailResponse:
    return await project_service.get_for_client(current_user.id, project_id)


@router.get("/project-status", response_model=ProjectStatusResponse)
async def my_project_status(
    current_user: User = Depends(require_client),
    project_service: ProjectService = Depends(get_project_service),
) -> ProjectStatusResponse:
    """Deprecated shim for old app builds: maps the caller's most-recently-
    updated non-archived Project onto the legacy flat shape. New builds call
    §5.1/§5.2 above instead. ProjectStatus / project_status itself is
    untouched."""
    return await project_service.legacy_status_for_client(current_user.id)


@router.get("/files")
async def my_files(current_user: User = Depends(require_client)) -> list[dict]:
    return []
