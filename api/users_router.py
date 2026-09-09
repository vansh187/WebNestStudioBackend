from fastapi import APIRouter, Depends, Query

from core.dependencies import get_current_user, get_messaging_service
from database.models import User
from schemas.messaging_schemas import UserSearchResponse
from services.messaging_service import MessagingService

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("/search", response_model=UserSearchResponse)
async def search_users(
    q: str = Query(min_length=1),
    limit: int = Query(default=20, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    messaging: MessagingService = Depends(get_messaging_service),
) -> UserSearchResponse:
    """Find registered, active WebNest Studio users by name or email, for the
    chat people-picker. Never returns the caller. `q` must be >= 2 real
    characters after trimming (the service enforces that with a 400)."""
    return await messaging.search_users(current_user, q, limit)
