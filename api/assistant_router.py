import uuid

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import ClientDisconnect
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from core.dependencies import get_assistant_service, get_current_user, get_db_session
from core.exceptions import PayloadTooLargeError
from database.models import User
from schemas.assistant_schemas import MAX_BODY_BYTES, AssistantUsageResponse, ChatRequest
from services.assistant_service import AssistantChatStream, AssistantRateLimitedError, AssistantService

router = APIRouter(prefix="/api/assistant", tags=["ai-assistant"])

_TOO_LARGE_MESSAGE = "This request is too large."


class AssistantStreamingResponse(StreamingResponse):
    """Closes the chat stream however the response ends: finished, failed, the client
    gone, even before the first byte when the generator never starts, or a client that
    stays connected but stops reading, which the time limit ends."""

    def __init__(self, chat: AssistantChatStream) -> None:
        super().__init__(
            chat.events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
        self._chat = chat

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            with anyio.move_on_after(self._chat.max_seconds) as limit:
                await super().__call__(scope, receive, send)
            if limit.cancelled_caught:
                self._chat.mark_timed_out()
        finally:
            # A disconnect cancels this task; usage must still be recorded.
            with anyio.CancelScope(shield=True):
                await self._chat.close()


async def get_assistant_user_id(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> uuid.UUID:
    """The logged-in user's id, with the login check's session handed back straight away.
    It would otherwise hold a database connection through a slow upload and the wait for
    Groq, while the limit check needs a second one, so a few slow requests could use up
    the pool for every route."""
    user_id = current_user.id
    await session.close()
    return user_id


async def _read_limited_body(request: Request) -> bytes:
    """FastAPI would read a declared body whole before any check, so it is read here
    and stopped as soon as it passes the limit."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise PayloadTooLargeError(_TOO_LARGE_MESSAGE)
    chunks: list[bytes] = []
    size = 0
    try:
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                raise PayloadTooLargeError(_TOO_LARGE_MESSAGE)
            chunks.append(chunk)
    except ClientDisconnect as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The request was not completed.") from exc
    return b"".join(chunks)


def _parse_chat_request(body: bytes) -> ChatRequest:
    try:
        return ChatRequest.model_validate_json(body)
    except PydanticValidationError as exc:
        # include_input=False: the input is the user's code, which must not be echoed.
        errors = exc.errors(include_url=False, include_input=False)
        raise RequestValidationError([{**error, "loc": ("body", *error["loc"])} for error in errors]) from exc


@router.post(
    "/chat",
    openapi_extra={
        "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object"}}}},
        "responses": {"200": {"description": "Server-sent events", "content": {"text/event-stream": {}}}},
    },
)
async def chat(
    request: Request,
    user_id: uuid.UUID = Depends(get_assistant_user_id),
    assistant_service: AssistantService = Depends(get_assistant_service),
) -> AssistantStreamingResponse:
    assistant_service.ensure_enabled()
    payload = _parse_chat_request(await _read_limited_body(request))
    try:
        stream = await assistant_service.open_chat(user_id, payload)
    except AssistantRateLimitedError as exc:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=exc.message, headers=headers) from exc
    return AssistantStreamingResponse(stream)


@router.get("/usage", response_model=AssistantUsageResponse)
async def usage(
    user_id: uuid.UUID = Depends(get_assistant_user_id),
    assistant_service: AssistantService = Depends(get_assistant_service),
) -> AssistantUsageResponse:
    return await assistant_service.get_usage(user_id)
