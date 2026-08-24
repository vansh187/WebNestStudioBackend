from fastapi import APIRouter, Depends, status

from core.dependencies import get_newsletter_service
from database.models import NewsletterSubscriber
from schemas.newsletter_schemas import NewsletterSubscribeRequest, NewsletterSubscribeResponse
from services.newsletter_service import NewsletterService

router = APIRouter(prefix="/api/newsletter", tags=["newsletter"])


@router.post("/subscribe", response_model=NewsletterSubscribeResponse, status_code=status.HTTP_201_CREATED)
async def subscribe(
    payload: NewsletterSubscribeRequest,
    newsletter_service: NewsletterService = Depends(get_newsletter_service),
) -> NewsletterSubscriber:
    return await newsletter_service.subscribe(payload.email, payload.source)
