import uuid

from fastapi import APIRouter, Depends

from core.dependencies import get_current_user, get_generation_service
from database.models import User
from schemas.generation_schemas import (
    GenerateRequest,
    GenerateResponse,
    GenerationMessageResponse,
    HistoryDetailResponse,
    HistoryListItem,
    HistoryListResponse,
    LimitStatusResponse,
    RefineRequest,
)
from services.generation_service import GenerationService

router = APIRouter(prefix="/api", tags=["ai-page-builder"])


@router.post("/generate", response_model=GenerateResponse)
async def generate_page(
    payload: GenerateRequest,
    current_user: User = Depends(get_current_user),
    generation_service: GenerationService = Depends(get_generation_service),
) -> GenerateResponse:
    generation, provider = await generation_service.generate(current_user.id, payload.prompt)
    return GenerateResponse(generation_id=generation.id, html=generation.latest_html, provider_used=provider)


@router.post("/generate/{generation_id}/refine", response_model=GenerateResponse)
async def refine_page(
    generation_id: uuid.UUID,
    payload: RefineRequest,
    current_user: User = Depends(get_current_user),
    generation_service: GenerationService = Depends(get_generation_service),
) -> GenerateResponse:
    generation, provider = await generation_service.refine(current_user.id, generation_id, payload.refinement)
    return GenerateResponse(generation_id=generation.id, html=generation.latest_html, provider_used=provider)


@router.get("/history", response_model=HistoryListResponse)
async def list_history(
    current_user: User = Depends(get_current_user),
    generation_service: GenerationService = Depends(get_generation_service),
) -> HistoryListResponse:
    rows = await generation_service.get_history_list(current_user.id)
    return HistoryListResponse(
        generations=[
            HistoryListItem(
                generation_id=generation.id,
                title=generation.title,
                created_at=generation.created_at,
                updated_at=generation.updated_at,
                message_count=message_count,
            )
            for generation, message_count in rows
        ]
    )


@router.get("/history/{generation_id}", response_model=HistoryDetailResponse)
async def get_history_detail(
    generation_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    generation_service: GenerationService = Depends(get_generation_service),
) -> HistoryDetailResponse:
    generation = await generation_service.get_thread_detail(current_user.id, generation_id)
    return HistoryDetailResponse(
        generation_id=generation.id,
        initial_prompt=generation.initial_prompt,
        latest_html=generation.latest_html,
        messages=[GenerationMessageResponse.model_validate(message) for message in generation.messages],
    )


@router.get("/generate/limit-status", response_model=LimitStatusResponse)
async def limit_status(
    current_user: User = Depends(get_current_user),
    generation_service: GenerationService = Depends(get_generation_service),
) -> LimitStatusResponse:
    remaining, limit, reset_at = await generation_service.get_limit_status(current_user.id)
    return LimitStatusResponse(remaining=remaining, limit=limit, reset_at=reset_at)
