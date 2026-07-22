from fastapi import APIRouter, Depends, Request, status

from core.dependencies import get_client_ip, get_lead_service
from database.models import Lead
from schemas.lead_schemas import LeadCreateRequest, LeadResponse
from services.lead_service import LeadService

router = APIRouter(prefix="/api/leads", tags=["leads"])


@router.post("", response_model=LeadResponse, status_code=status.HTTP_201_CREATED)
async def create_lead(
    payload: LeadCreateRequest,
    request: Request,
    lead_service: LeadService = Depends(get_lead_service),
    client_ip: str | None = Depends(get_client_ip),
) -> Lead:
    return await lead_service.create_lead(
        ip_address=client_ip,
        user_agent=request.headers.get("user-agent"),
        **payload.model_dump(),
    )
