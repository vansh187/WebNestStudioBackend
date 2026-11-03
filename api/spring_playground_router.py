from fastapi import APIRouter, Depends, Response

from core.dependencies import get_current_user, get_spring_playground_service
from database.models import User
from schemas.spring_playground_schemas import SpringRunRequest
from services.spring_playground_service import SpringPlaygroundService

router = APIRouter(prefix="/api/spring", tags=["spring-playground"])


@router.post("/run")
async def run_spring_project(
    payload: SpringRunRequest,
    current_user: User = Depends(get_current_user),
    spring_playground_service: SpringPlaygroundService = Depends(get_spring_playground_service),
) -> Response:
    # Logged-in users only. The playground's JSON is passed through byte-for-byte.
    body = await spring_playground_service.run(payload, current_user)
    return Response(content=body, media_type="application/json")
