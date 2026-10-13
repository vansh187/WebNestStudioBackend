from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from core.dependencies import get_current_user, get_db_session
from database.device_token_persistence import DeviceTokenPersistence
from database.models import User
from schemas.device_schemas import DeviceTokenDeleteRequest, DeviceTokenRequest

router = APIRouter(prefix="/api/me/devices", tags=["push-notifications"])


@router.post("", status_code=status.HTTP_204_NO_CONTENT)
async def register_device(
    payload: DeviceTokenRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    """Registers this device's FCM token for the signed-in user. Idempotent;
    the app calls it on every sign-in, app start and token rotation."""
    await DeviceTokenPersistence(session).upsert(current_user.id, payload.token.strip(), payload.platform)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/unregister", status_code=status.HTTP_204_NO_CONTENT)
async def unregister_device(
    payload: DeviceTokenDeleteRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    """Stops pushes to this device for the signed-in user (called on logout)."""
    await DeviceTokenPersistence(session).delete_for_user(current_user.id, payload.token.strip())
    return Response(status_code=status.HTTP_204_NO_CONTENT)
