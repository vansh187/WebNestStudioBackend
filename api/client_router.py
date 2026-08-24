from fastapi import APIRouter, Depends

from core.dependencies import get_client_service, require_client
from database.models import ProjectStatus, User
from schemas.client_schemas import ProjectStatusResponse
from services.client_service import ClientService

router = APIRouter(prefix="/api/me", tags=["client"])


@router.get("/project-status", response_model=ProjectStatusResponse)
async def my_project_status(
    current_user: User = Depends(require_client),
    client_service: ClientService = Depends(get_client_service),
) -> ProjectStatus:
    return await client_service.get_project_status(current_user.id)


@router.get("/files")
async def my_files(current_user: User = Depends(require_client)) -> list[dict]:
    return []
