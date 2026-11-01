import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import ValidationError as PydanticValidationError
from starlette.requests import ClientDisconnect

from api.assistant_router import get_assistant_user_id
from core.dependencies import get_assistant_payment_service
from core.exceptions import PayloadTooLargeError
from schemas.assistant_schemas import AccessResponse, MembershipResponse, OrderResponse, VerifyRequest, VerifyResponse
from services.assistant_payment_service import (
    AssistantPaymentService,
    InvalidWebhookSignatureError,
    WebhookNotConfiguredError,
)

router = APIRouter(prefix="/api/assistant", tags=["ai-assistant-payments"])
webhook_router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])

MAX_VERIFY_BODY_BYTES = 4096
MAX_WEBHOOK_BODY_BYTES = 1024 * 1024
_NOT_VERIFIED = "This payment could not be verified."


async def _read_body(request: Request, limit: int) -> bytes:
    """The raw bytes, stopped as soon as they pass the limit. The webhook's signature is
    over exactly these bytes, so nothing may parse or re-encode them first."""
    chunks: list[bytes] = []
    size = 0
    try:
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                raise PayloadTooLargeError("This request is too large.")
            chunks.append(chunk)
    except ClientDisconnect as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The request was not completed.") from exc
    return b"".join(chunks)


@router.get("/membership", response_model=MembershipResponse)
async def membership(
    user_id: uuid.UUID = Depends(get_assistant_user_id),
    payments: AssistantPaymentService = Depends(get_assistant_payment_service),
) -> MembershipResponse:
    return await payments.get_membership(user_id)


@router.post("/payments/order", response_model=OrderResponse)
async def create_order(
    user_id: uuid.UUID = Depends(get_assistant_user_id),
    payments: AssistantPaymentService = Depends(get_assistant_payment_service),
) -> OrderResponse:
    return await payments.create_order(user_id)


@router.post(
    "/payments/verify",
    response_model=VerifyResponse,
    openapi_extra={"requestBody": {"required": True, "content": {"application/json": {"schema": VerifyRequest.model_json_schema()}}}},
)
async def verify_payment(
    request: Request,
    user_id: uuid.UUID = Depends(get_assistant_user_id),
    payments: AssistantPaymentService = Depends(get_assistant_payment_service),
) -> VerifyResponse:
    raw = await _read_body(request, MAX_VERIFY_BODY_BYTES)
    try:
        payload = VerifyRequest.model_validate_json(raw)
    except PydanticValidationError as exc:
        # 400, not 422: the frontend's contract, and its input must not be echoed back.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_NOT_VERIFIED) from exc
    access: AccessResponse = await payments.verify(user_id, payload)
    return VerifyResponse(access=access)


@webhook_router.post("/razorpay", include_in_schema=False)
async def razorpay_webhook(
    request: Request,
    payments: AssistantPaymentService = Depends(get_assistant_payment_service),
) -> dict[str, str]:
    raw = await _read_body(request, MAX_WEBHOOK_BODY_BYTES)
    signature = request.headers.get("x-razorpay-signature", "")
    event_id = request.headers.get("x-razorpay-event-id", "")
    try:
        await payments.handle_webhook(raw, signature, event_id)
    except InvalidWebhookSignatureError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid signature.") from exc
    except WebhookNotConfiguredError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Webhook is not configured.") from exc
    return {"status": "ok"}
