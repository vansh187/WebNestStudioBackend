import json
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Response

from core.dependencies import (
    get_chat_orchestrator_service,
    get_chatbot_service,
    get_current_user,
    get_email_service,
    get_plan_pdf_service,
)
from database.models import User
from schemas.chat_schemas import (
    ChatHistoryItem,
    ChatMessageRequest,
    ChatMessageResponse,
    ChatStartResponse,
    ChatThreadDetailResponse,
    ChatThreadListItem,
    ChatThreadListResponse,
)
from schemas.chatbot_schemas import (
    ChatbotLimitStatusResponse,
    CollectedFields,
    EmailPlanRequest,
    EmailPlanResponse,
    GeneratePlanResponse,
    PlanWeek,
)
from services.chat_orchestrator_service import ChatOrchestratorService
from services.chatbot_service import ChatbotService
from services.email_service import EmailService
from services.plan_pdf_service import PlanPdfService

router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.post("/threads", response_model=ChatStartResponse)
async def start_thread(
    current_user: User = Depends(get_current_user),
    orchestrator: ChatOrchestratorService = Depends(get_chat_orchestrator_service),
) -> ChatStartResponse:
    thread, welcome = await orchestrator.start_thread(current_user)
    return ChatStartResponse(thread_id=thread.id, mode=thread.mode, reply=welcome)


@router.post("/threads/{thread_id}/messages", response_model=ChatMessageResponse)
async def send_message(
    thread_id: uuid.UUID,
    payload: ChatMessageRequest,
    current_user: User = Depends(get_current_user),
    orchestrator: ChatOrchestratorService = Depends(get_chat_orchestrator_service),
) -> ChatMessageResponse:
    result = await orchestrator.send_message(current_user, thread_id, payload.message)
    collected_fields = result.get("collected_fields")
    return ChatMessageResponse(
        thread_id=thread_id,
        mode=result["mode"],
        reply=result.get("reply"),
        html=result.get("html"),
        generation_id=result.get("generation_id"),
        collected_fields=CollectedFields(**collected_fields) if collected_fields is not None else None,
        ready_for_plan=result.get("ready_for_plan"),
    )


@router.get("/threads", response_model=ChatThreadListResponse)
async def list_threads(
    current_user: User = Depends(get_current_user),
    orchestrator: ChatOrchestratorService = Depends(get_chat_orchestrator_service),
) -> ChatThreadListResponse:
    rows = await orchestrator.list_threads(current_user.id)
    return ChatThreadListResponse(
        threads=[
            ChatThreadListItem(
                thread_id=thread.id,
                mode=thread.mode,
                created_at=thread.created_at,
                updated_at=thread.updated_at,
                message_count=message_count,
            )
            for thread, message_count in rows
        ]
    )


@router.get("/threads/{thread_id}", response_model=ChatThreadDetailResponse)
async def get_thread_detail(
    thread_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    orchestrator: ChatOrchestratorService = Depends(get_chat_orchestrator_service),
) -> ChatThreadDetailResponse:
    thread = await orchestrator.get_thread_detail(current_user.id, thread_id)
    return ChatThreadDetailResponse(
        thread_id=thread.id,
        mode=thread.mode,
        messages=[ChatHistoryItem.model_validate(message) for message in thread.messages],
    )


@router.post("/threads/{thread_id}/generate-plan", response_model=GeneratePlanResponse)
async def generate_plan(
    thread_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    orchestrator: ChatOrchestratorService = Depends(get_chat_orchestrator_service),
    email_service: EmailService = Depends(get_email_service),
    pdf_service: PlanPdfService = Depends(get_plan_pdf_service),
) -> GeneratePlanResponse:
    plan, lead_id, lead_created = await orchestrator.generate_plan(current_user, thread_id)
    weeks = json.loads(plan.plan_weeks_json)
    html_summary = pdf_service.render_plan_html_summary(plan.project_type, plan.total_weeks, weeks)

    # Only ping the team on the first plan for this thread - regenerating an
    # existing plan updates the same Lead, so notifying again would look like
    # a duplicate/new lead in the team's inbox.
    if lead_created:
        background_tasks.add_task(
            email_service.send_lead_notification_email,
            full_name=current_user.full_name,
            email=current_user.email,
            phone_number=current_user.phone_number,
            source="chatbot",
            message=f"New chatbot plan generated: {plan.project_type}",
        )

    return GeneratePlanResponse(
        thread_id=thread_id,
        plan_id=plan.id,
        project_type=plan.project_type,
        total_weeks=plan.total_weeks,
        weeks=[PlanWeek(**week) for week in weeks],
        html_summary=html_summary,
        pdf_download_url=f"/api/chat/plans/{plan.id}/pdf",
        lead_id=lead_id,
    )


@router.get("/plans/{plan_id}/pdf")
async def download_plan_pdf(
    plan_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    chatbot_service: ChatbotService = Depends(get_chatbot_service),
    pdf_service: PlanPdfService = Depends(get_plan_pdf_service),
) -> Response:
    plan = await chatbot_service.get_plan(current_user.id, plan_id)
    weeks = json.loads(plan.plan_weeks_json)
    pdf_bytes = pdf_service.render_plan_pdf(plan.project_type, plan.total_weeks, weeks, current_user.full_name)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="webnest-studio-plan.pdf"'},
    )


@router.post("/plans/{plan_id}/email", response_model=EmailPlanResponse)
async def email_plan(
    plan_id: uuid.UUID,
    payload: EmailPlanRequest,
    current_user: User = Depends(get_current_user),
    chatbot_service: ChatbotService = Depends(get_chatbot_service),
    pdf_service: PlanPdfService = Depends(get_plan_pdf_service),
    email_service: EmailService = Depends(get_email_service),
) -> EmailPlanResponse:
    plan = await chatbot_service.get_plan(current_user.id, plan_id)
    # Bounds how many emails (to an arbitrary to_address) an authenticated
    # user can trigger via this endpoint - without this, it's an open vector
    # to spam arbitrary addresses through WebNest Studio's own email account.
    await chatbot_service.check_and_consume_rate_limit(current_user.id)
    weeks = json.loads(plan.plan_weeks_json)
    pdf_bytes = pdf_service.render_plan_pdf(plan.project_type, plan.total_weeks, weeks, current_user.full_name)
    to_address = payload.to_address or current_user.email
    sent = await email_service.send_plan_email(to_address, plan.project_type, pdf_bytes)
    return EmailPlanResponse(sent=sent)


@router.get("/limit-status", response_model=ChatbotLimitStatusResponse)
async def limit_status(
    current_user: User = Depends(get_current_user),
    chatbot_service: ChatbotService = Depends(get_chatbot_service),
) -> ChatbotLimitStatusResponse:
    remaining, limit, reset_at = await chatbot_service.get_limit_status(current_user.id)
    return ChatbotLimitStatusResponse(remaining=remaining, limit=limit, reset_at=reset_at)
