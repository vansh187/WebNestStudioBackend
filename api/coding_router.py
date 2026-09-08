import uuid

from fastapi import APIRouter, Depends, Query, Request, Response, status

from core.dependencies import get_client_ip, get_coding_service, get_current_user, get_optional_current_user
from database.models import User
from schemas.coding_schemas import (
    CodingStatsResponse,
    CompilerLanguage,
    ExecuteRequest,
    ExecuteResponse,
    ProjectCreateRequest,
    ProjectListItem,
    ProjectListResponse,
    ProjectResponse,
    ProjectUpdateRequest,
    ShareCreateRequest,
    ShareCreateResponse,
    ShareResponse,
)
from services.coding_service import CodingService

router = APIRouter(prefix="/api", tags=["coding-platform"])


@router.get("/compiler/languages", response_model=list[CompilerLanguage])
async def list_compiler_languages(coding_service: CodingService = Depends(get_coding_service)) -> list[dict]:
    return coding_service.list_languages()


@router.post("/compiler/execute", response_model=ExecuteResponse)
async def execute_code(
    payload: ExecuteRequest,
    request: Request,
    current_user: User | None = Depends(get_optional_current_user),
    coding_service: CodingService = Depends(get_coding_service),
) -> ExecuteResponse:
    return await coding_service.execute(payload, current_user, get_client_ip(request))


@router.get("/projects", response_model=ProjectListResponse)
async def list_projects(
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = None,
    current_user: User = Depends(get_current_user),
    coding_service: CodingService = Depends(get_coding_service),
) -> ProjectListResponse:
    projects, next_cursor = await coding_service.list_projects(current_user.id, limit, cursor)
    return ProjectListResponse(
        items=[ProjectListItem.model_validate(project) for project in projects],
        next_cursor=next_cursor,
    )


@router.post("/projects", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: ProjectCreateRequest,
    current_user: User = Depends(get_current_user),
    coding_service: CodingService = Depends(get_coding_service),
) -> ProjectResponse:
    project = await coding_service.create_project(
        user_id=current_user.id,
        title=payload.title,
        language=payload.language,
        files=payload.files,
        source=payload.source,
        stdin=payload.stdin,
        description=payload.description,
    )
    return ProjectResponse.model_validate(project)


@router.get("/projects/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    coding_service: CodingService = Depends(get_coding_service),
) -> ProjectResponse:
    project = await coding_service.get_project(current_user.id, project_id)
    return ProjectResponse.model_validate(project)


@router.put("/projects/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: uuid.UUID,
    payload: ProjectUpdateRequest,
    current_user: User = Depends(get_current_user),
    coding_service: CodingService = Depends(get_coding_service),
) -> ProjectResponse:
    project = await coding_service.update_project(
        current_user.id,
        project_id,
        payload.model_dump(exclude_unset=True),
    )
    return ProjectResponse.model_validate(project)


@router.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    coding_service: CodingService = Depends(get_coding_service),
) -> Response:
    await coding_service.delete_project(current_user.id, project_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/shares", response_model=ShareCreateResponse)
async def create_share(
    payload: ShareCreateRequest,
    current_user: User = Depends(get_current_user),
    coding_service: CodingService = Depends(get_coding_service),
) -> ShareCreateResponse:
    share = await coding_service.create_share(
        user=current_user,
        project_id=payload.project_id,
        title=payload.title,
        language=payload.language,
        files=payload.files,
        source=payload.source,
        stdin=payload.stdin,
        stdout=payload.stdout,
    )
    return ShareCreateResponse(share_id=share.share_id, url=f"/s/{share.share_id}")


@router.get("/shares/{share_id}", response_model=ShareResponse)
async def get_share(
    share_id: str,
    coding_service: CodingService = Depends(get_coding_service),
) -> ShareResponse:
    share = await coding_service.get_share(share_id)
    return ShareResponse.model_validate(share)


@router.get("/me/coding-stats", response_model=CodingStatsResponse)
async def get_coding_stats(
    current_user: User = Depends(get_current_user),
    coding_service: CodingService = Depends(get_coding_service),
) -> CodingStatsResponse:
    projects_count, last_activity_at = await coding_service.get_stats(current_user.id)
    return CodingStatsResponse(projects_count=projects_count, last_activity_at=last_activity_at)
