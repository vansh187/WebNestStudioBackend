"""WebNest AI for VS Code: /api/extension/* (VSCODE_EXTENSION_BACKEND.md section 2).

Every refusal is {"detail", "code", "retry_after"?, "request_id"} (core/extension_http.py), and
no route echoes its input in an error: a request body holds the user's code. Bodies are read
with a hard size limit before parsing. While EXTENSION_ENABLED is false every route here
answers 503, and nothing else in the API is affected."""

import logging
import uuid
from typing import TypeVar

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from api.assistant_router import AssistantStreamingResponse
from core.config import Settings
from core.dependencies import (
    bearer_scheme,
    get_app_settings,
    get_assistant_report_service,
    get_auth_service,
    get_client_ip,
    get_db_session,
    get_extension_auth_service,
    get_extension_chat_service,
    get_extension_rate_limits,
)
from core.exceptions import DomainError, ExtensionError, UnauthorizedError
from core.extension_http import request_id, validation_errors
from core.request_limits import read_limited_body
from database.extension_session_persistence import SessionUser
from schemas.assistant_schemas import AssistantReportCreate, AssistantReportCreated
from schemas.extension_schemas import (
    MAX_AUTH_BODY_BYTES,
    MAX_BODY_BYTES,
    CodeRequest,
    CodeResponse,
    ExtensionChatRequest,
    MeResponse,
    RefreshRequest,
    SessionList,
    TokenRequest,
    TokenResponse,
)
from services.assistant_report_service import AssistantReportService
from services.auth_service import AuthService
from services.extension_auth_service import ExtensionAuthService, client_version, parse_version
from services.extension_chat import ExtensionChatService, start_event
from services.extension_rate_limits import ExtensionRateLimits

logger = logging.getLogger("webnest.extension")

_SWITCHED_OFF = "WebNest AI for VS Code is coming soon."
_SWITCHED_OFF_RETRY_SECONDS = 60
_OUTDATED = "Please update WebNest AI for VS Code to keep using it."
_LOG_IN_ON_WEBSITE = "Please log in on the WebNest website first."
_LOG_IN = "Please log in to WebNest AI."
_TOO_MANY = "Too many requests. Please wait a moment and try again."
_TOO_LARGE = "This request is too large."

Model = TypeVar("Model", bound=BaseModel)


def require_extension_enabled(settings: Settings = Depends(get_app_settings)) -> None:
    """The master switch, so all of this can be deployed dark and switched on without a redeploy."""
    if not settings.extension_enabled:
        raise ExtensionError(503, "service_unavailable", _SWITCHED_OFF, _SWITCHED_OFF_RETRY_SECONDS)


def require_supported_client(request: Request, settings: Settings = Depends(get_app_settings)) -> None:
    """426 for an extension older than EXTENSION_MIN_VERSION. A caller that names no version
    (the website, curl) has nothing to compare."""
    sent = client_version(request.headers.get("x-webnest-client"))
    minimum = parse_version(settings.extension_min_version)
    if sent is not None and minimum is not None and sent < minimum:
        raise ExtensionError(426, "extension_outdated", _OUTDATED)


router = APIRouter(prefix="/api/extension", tags=["vscode-extension"], dependencies=[Depends(require_extension_enabled)])


async def website_user_id(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    auth_service: AuthService = Depends(get_auth_service),
    session: AsyncSession = Depends(get_db_session),
) -> uuid.UUID:
    """The website's logged-in user, from its own access token. A VS Code token is refused
    (it carries a scope), so a stolen one can't connect or list devices. The login check's
    database session is handed back at once."""
    if credentials is None or not credentials.credentials:
        raise ExtensionError(401, "not_authenticated", _LOG_IN_ON_WEBSITE)
    try:
        user = await auth_service.get_current_user(credentials.credentials)
        user_id = user.id
    except (UnauthorizedError, ValueError) as exc:
        raise ExtensionError(401, "not_authenticated", _LOG_IN_ON_WEBSITE) from exc
    finally:
        await session.close()
    return user_id


async def extension_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    auth: ExtensionAuthService = Depends(get_extension_auth_service),
) -> SessionUser:
    """A VS Code access token with a live session; its own short database session is
    already handed back when this returns."""
    if credentials is None or not credentials.credentials:
        raise ExtensionError(401, "not_authenticated", _LOG_IN)
    return await auth.authenticate(credentials.credentials)


def _parse(model: type[Model], raw: bytes) -> Model:
    try:
        return model.model_validate_json(raw)
    except PydanticValidationError as exc:
        errors = exc.errors(include_url=False, include_input=False, include_context=False)
        located = [{**error, "loc": ("body", *error["loc"])} for error in errors]
        raise ExtensionError(422, "validation_error", "Request validation failed", errors=validation_errors(located)) from exc


def _ip_key(client_ip: str | None) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"webnest-extension-ip:{client_ip or 'unknown'}")


def _limit(limits: ExtensionRateLimits, name: str, key: uuid.UUID) -> None:
    if not limits.allow(name, key):
        raise ExtensionError(429, "rate_limited", _TOO_MANY, limits.window_seconds(name))


# ---- 2.1, 2.3, 2.4, 2.5 login -------------------------------------------------------------


@router.post(
    "/auth/codes",
    status_code=status.HTTP_201_CREATED,
    response_model=CodeResponse,
    openapi_extra={"requestBody": {"required": True, "content": {"application/json": {"schema": CodeRequest.model_json_schema()}}}},
)
async def create_code(
    request: Request,
    user_id: uuid.UUID = Depends(website_user_id),
    auth: ExtensionAuthService = Depends(get_extension_auth_service),
    limits: ExtensionRateLimits = Depends(get_extension_rate_limits),
) -> CodeResponse:
    _limit(limits, "codes", user_id)
    payload = _parse(CodeRequest, await read_limited_body(request, MAX_AUTH_BODY_BYTES, _TOO_LARGE))
    return await auth.create_code(user_id, payload)


@router.post(
    "/auth/token",
    response_model=TokenResponse,
    openapi_extra={"requestBody": {"required": True, "content": {"application/json": {"schema": TokenRequest.model_json_schema()}}}},
)
async def exchange_code(
    request: Request,
    client_ip: str | None = Depends(get_client_ip),
    auth: ExtensionAuthService = Depends(get_extension_auth_service),
    limits: ExtensionRateLimits = Depends(get_extension_rate_limits),
) -> TokenResponse:
    _limit(limits, "token", _ip_key(client_ip))
    payload = _parse(TokenRequest, await read_limited_body(request, MAX_AUTH_BODY_BYTES, _TOO_LARGE))
    return await auth.exchange(payload)


@router.post(
    "/auth/refresh",
    response_model=TokenResponse,
    openapi_extra={"requestBody": {"required": True, "content": {"application/json": {"schema": RefreshRequest.model_json_schema()}}}},
)
async def refresh_tokens(
    request: Request,
    client_ip: str | None = Depends(get_client_ip),
    auth: ExtensionAuthService = Depends(get_extension_auth_service),
    limits: ExtensionRateLimits = Depends(get_extension_rate_limits),
) -> TokenResponse:
    _limit(limits, "refresh", _ip_key(client_ip))
    payload = _parse(RefreshRequest, await read_limited_body(request, MAX_AUTH_BODY_BYTES, _TOO_LARGE))
    return await auth.refresh(payload.refresh_token, request.headers.get("x-webnest-client"))


@router.post(
    "/auth/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    openapi_extra={"requestBody": {"required": True, "content": {"application/json": {"schema": RefreshRequest.model_json_schema()}}}},
)
async def logout(
    request: Request,
    client_ip: str | None = Depends(get_client_ip),
    auth: ExtensionAuthService = Depends(get_extension_auth_service),
    limits: ExtensionRateLimits = Depends(get_extension_rate_limits),
) -> Response:
    """Always 204: the extension has already signed out locally. A bad body, a flood of calls
    or a database failure only means there is no session to end here."""
    try:
        if limits.allow("logout", _ip_key(client_ip)):
            payload = RefreshRequest.model_validate_json(await read_limited_body(request, MAX_AUTH_BODY_BYTES, _TOO_LARGE))
            await auth.logout(payload.refresh_token)
    except (DomainError, PydanticValidationError, ValueError) as exc:
        logger.info("VS Code logout with nothing to end: %s", type(exc).__name__)
    except Exception as exc:
        logger.error("VS Code logout failed: %s", type(exc).__name__)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---- 2.6 me, 2.7 chat, 2.8 reports ----------------------------------------------------------


@router.get("/me", response_model=MeResponse, dependencies=[Depends(require_supported_client)])
async def me(
    session_user: SessionUser = Depends(extension_user),
    service: ExtensionChatService = Depends(get_extension_chat_service),
    limits: ExtensionRateLimits = Depends(get_extension_rate_limits),
) -> MeResponse:
    _limit(limits, "me", session_user.user_id)
    return await service.me(session_user)


@router.post(
    "/chat",
    dependencies=[Depends(require_supported_client)],
    openapi_extra={
        "requestBody": {"required": True, "content": {"application/json": {"schema": ExtensionChatRequest.model_json_schema()}}},
        "responses": {"200": {"description": "Server-sent events", "content": {"text/event-stream": {}}}},
    },
)
async def chat(
    request: Request,
    session_user: SessionUser = Depends(extension_user),
    service: ExtensionChatService = Depends(get_extension_chat_service),
    limits: ExtensionRateLimits = Depends(get_extension_rate_limits),
) -> AssistantStreamingResponse:
    user_id = session_user.user_id
    _limit(limits, "chat", user_id)
    payload = _parse(ExtensionChatRequest, await read_limited_body(request, MAX_BODY_BYTES, _TOO_LARGE))
    stream = await service.open_chat(user_id, payload)
    try:
        # From here the response owns the stream and closes it however the reply ends.
        return AssistantStreamingResponse(stream, first=start_event(request_id(request)))
    except BaseException:
        await stream.close()
        raise


@router.post(
    "/reports",
    status_code=status.HTTP_201_CREATED,
    response_model=AssistantReportCreated,
    openapi_extra={"requestBody": {"required": True, "content": {"application/json": {"schema": AssistantReportCreate.model_json_schema()}}}},
)
async def report_reply(
    request: Request,
    session_user: SessionUser = Depends(extension_user),
    reports: AssistantReportService = Depends(get_assistant_report_service),
) -> AssistantReportCreated:
    payload = _parse(AssistantReportCreate, await read_limited_body(request, MAX_BODY_BYTES, "This report is too large."))
    return await reports.report(session_user.user_id, payload)


# ---- 2.9 connected devices (website) --------------------------------------------------------


@router.get("/sessions", response_model=SessionList)
async def list_sessions(
    user_id: uuid.UUID = Depends(website_user_id),
    auth: ExtensionAuthService = Depends(get_extension_auth_service),
) -> SessionList:
    return await auth.list_sessions(user_id)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def disconnect_session(
    session_id: str,
    user_id: uuid.UUID = Depends(website_user_id),
    auth: ExtensionAuthService = Depends(get_extension_auth_service),
) -> Response:
    await auth.disconnect(user_id, session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
