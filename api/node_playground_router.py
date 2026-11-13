from fastapi import APIRouter, Depends, Response

from core.dependencies import get_current_user, get_node_playground_service
from database.models import User
from schemas.node_playground_schemas import NodeProjectRunRequest, NodeRunRequest
from services.node_playground_service import NodePlaygroundService

router = APIRouter(prefix="/api/node", tags=["node-playground"])


@router.post("/run")
async def run_node_script(
    payload: NodeRunRequest,
    current_user: User = Depends(get_current_user),
    node_playground_service: NodePlaygroundService = Depends(get_node_playground_service),
) -> Response:
    # Logged-in users only. The playground's JSON is passed through byte-for-byte.
    body = await node_playground_service.run_script(payload, current_user)
    return Response(content=body, media_type="application/json")


@router.post("/project/run")
async def run_node_project(
    payload: NodeProjectRunRequest,
    current_user: User = Depends(get_current_user),
    node_playground_service: NodePlaygroundService = Depends(get_node_playground_service),
) -> Response:
    body = await node_playground_service.run_project(payload, current_user)
    return Response(content=body, media_type="application/json")


@router.get("/packages")
async def list_node_packages(
    node_playground_service: NodePlaygroundService = Depends(get_node_playground_service),
) -> Response:
    # Public: the names and versions of the preinstalled packages, cached for an hour.
    body = await node_playground_service.packages()
    return Response(content=body, media_type="application/json", headers={"Cache-Control": "public, max-age=3600"})
