from fastapi import APIRouter, BackgroundTasks, Depends, Request, status

from core.dependencies import get_client_ip, get_email_service, get_lead_service
from database.models import Lead
from schemas.lead_schemas import LeadCreateRequest, LeadResponse
from services.email_service import EmailService
from services.lead_service import LeadService

router = APIRouter(prefix="/api/leads", tags=["leads"])


@router.post("", response_model=LeadResponse, status_code=status.HTTP_201_CREATED)
async def create_lead(
    payload: LeadCreateRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    lead_service: LeadService = Depends(get_lead_service),
    email_service: EmailService = Depends(get_email_service),
    client_ip: str | None = Depends(get_client_ip),
) -> Lead:
    lead = await lead_service.create_lead(
        ip_address=client_ip,
        user_agent=request.headers.get("user-agent"),
        **payload.model_dump(),
    )
    # Team notification email is a slow, best-effort network call - it must
    # never block this response (Render's outbound SMTP can hang for tens of seconds).
    background_tasks.add_task(
        email_service.send_lead_notification_email,
        full_name=lead.full_name,
        email=lead.email,
        phone_number=lead.phone_number,
        source=lead.source,
        message=lead.message,
    )
    return lead
