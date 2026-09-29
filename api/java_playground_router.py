import logging

from fastapi import APIRouter, Depends, Request, Response
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from core.dependencies import (
    bearer_scheme,
    get_auth_service,
    get_client_ip,
    get_db_session,
    get_java_playground_service,
)
from database.models import User
from schemas.java_playground_schemas import JavaRunRequest
from services.auth_service import AuthService
from services.java_playground_service import JavaPlaygroundService

logger = logging.getLogger("webnest.java_playground")

router = APIRouter(prefix="/api/java", tags=["java-playground"])


async def _optional_visitor(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    auth_service: AuthService = Depends(get_auth_service),
    session: AsyncSession = Depends(get_db_session),
) -> User | None:
    # Like get_optional_current_user, but a public route must never fail on the
    # caller's token: any problem (expired, malformed, DB hiccup) means anonymous.
    if credentials is None or not credentials.credentials:
        return None
    try:
        return await auth_service.get_current_user(credentials.credentials)
    except Exception as exc:
        logger.debug("Treating Java playground caller as anonymous: %s", type(exc).__name__)
        # A failed lookup can leave the request's shared session in an aborted
        # transaction; reset it so the daily-cap query still works.
        try:
            await session.rollback()
        except Exception:
            logger.warning("Could not roll back the session after a failed visitor lookup", exc_info=True)
        return None


@router.post("/run")
async def run_java(
    payload: JavaRunRequest,
    request: Request,
    current_user: User | None = Depends(_optional_visitor),
    java_playground_service: JavaPlaygroundService = Depends(get_java_playground_service),
) -> Response:
    # Public route: the playground's JSON is passed through byte-for-byte.
    body = await java_playground_service.run(payload, current_user, get_client_ip(request))
    return Response(content=body, media_type="application/json")
