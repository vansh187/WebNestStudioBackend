"""WebNest Prime bought through Google Play Billing in the Android app. Only Google's answer
counts: the app's word on the product, order, owner or success is never used. Each purchase
token grants once, to one account, through the same access grant as Razorpay.

Never logs a whole purchase token, only its last 8 characters, and never passes Google's
error text to the app. A temporary problem is always a 429 or 503, never a 4xx the app reads
as a failed payment, because the customer may well have paid.

WebNest offers no refunds (the owner's decision), so nothing here takes a pass back: there is
no voided-purchase check and no refund notification, and a granted token stays granted. That
is why acknowledging matters: an unacknowledged purchase is refunded by Google after 3 days."""

import asyncio
import logging
import re
import uuid
from datetime import datetime

from core.config import Settings
from core.exceptions import BadRequestError, ConflictError, DatabaseError, ExternalServiceError, ForbiddenError, NotFoundError, RateLimitedError
from database.assistant_payment_persistence import NewPlayPurchase, PlayPurchaseStore
from schemas.assistant_schemas import AccessResponse, GooglePlayVerifyRequest
from services.assistant_payment_service import AssistantPaymentService
from services.google_play_client import (
    GooglePlayClient,
    GooglePlayUnavailableError,
    PlayAcknowledgeRefusedError,
    PlayPurchase,
    PlayPurchaseNotFoundError,
    token_tail,
)
from services.user_rate_limiter import UserRateLimiter

logger = logging.getLogger("webnest.assistant.payments")

VERIFY_ATTEMPTS_PER_MINUTE = 10
# Google's own call is kept well under the app's 30-second timeout.
_GOOGLE_SECONDS = 10.0
_ACKNOWLEDGE_SECONDS = 5.0
_PURCHASED, _PENDING = 0, 2
_ACKNOWLEDGED = 1
_CONSUMED = 1
# Google's tokens are letters, digits and a few separators. Anything else isn't one, and is
# refused before it reaches a URL or the logs.
_TOKEN = re.compile(r"[A-Za-z0-9._:-]{1,4096}")
_MAX_TERMS_VERSION_CHARS = 64
_LOG_CHARS = 64

_UNAVAILABLE = "Payments are not available right now. Please try again later."
_NOT_VERIFIED = "This purchase could not be verified."
_OTHER_ACCOUNT = "This purchase belongs to a different WebNest account."
_ALREADY_USED = "This purchase has already been used by another WebNest account."


def _same_account(account_id: str | None, user_id: uuid.UUID) -> bool:
    """The app sets the purchase's obfuscated account id to the user's id when it buys."""
    if account_id is None:
        return False
    try:
        return uuid.UUID(account_id) == user_id
    except ValueError:
        return False


def _terms_version(value: str | None) -> str | None:
    return value if value and "\x00" not in value and len(value) <= _MAX_TERMS_VERSION_CHARS else None


def _terms_time(value: str | None) -> datetime | None:
    """When the user ticked the terms. Kept only if it is a real time with a zone, since it
    is a record of consent; a bad one is dropped rather than failing the purchase."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _log_value(value: str | None) -> str:
    return repr(value[:_LOG_CHARS]) if value else "-"


class GooglePlayPaymentService:
    def __init__(
        self,
        settings: Settings,
        store: PlayPurchaseStore,
        client: GooglePlayClient,
        access: AssistantPaymentService,
        limiter: UserRateLimiter,
    ) -> None:
        self._settings = settings
        self._store = store
        self._client = client
        self._access = access
        self._limiter = limiter

    async def verify(self, user_id: uuid.UUID, payload: GooglePlayVerifyRequest) -> AccessResponse | None:
        """The user's access once this purchase is granted, or None while Google still reports
        it as pending, which the router sends as a 202."""
        # As the spec requires. With the paywall off the assistant is free, so a purchase made
        # then is left unacknowledged on purpose and Google refunds it.
        if not self._settings.assistant_paywall_enabled or not self._client.configured:
            raise ExternalServiceError(_UNAVAILABLE)
        product_id = self._settings.assistant_google_play_product_id
        if payload.product_id != product_id:
            logger.warning("Google Play verify for an unknown product: product=%s user=%s", _log_value(payload.product_id), user_id)
            raise NotFoundError("Unknown product.")
        token = payload.purchase_token or ""
        if not _TOKEN.fullmatch(token):
            logger.warning("Google Play verify without a usable purchase token: user=%s", user_id)
            raise BadRequestError(_NOT_VERIFIED)
        if not self._limiter.allow(user_id):
            raise RateLimitedError("Too many attempts. Please wait a minute and try again.")

        already = await self._granted_before(token, user_id)
        if already is not None:
            return already
        purchase = await self._read_purchase(product_id, token, user_id)
        if purchase is None:
            return None
        claimed = await self._store.claim_play_purchase(
            NewPlayPurchase(
                user_id=user_id,
                purchase_token=token,
                product_id=product_id,
                google_order_id=purchase.order_id,
                pass_days=self._settings.assistant_pass_days,
                pass_tokens=self._settings.assistant_pass_tokens,
                region_code=purchase.region_code,
                purchase_time=purchase.purchase_time,
                terms_version=_terms_version(payload.terms_version),
                terms_accepted_at=_terms_time(payload.terms_accepted_at),
                acknowledged=purchase.acknowledgement_state == _ACKNOWLEDGED,
            )
        )
        if not claimed:
            # Another request granted this token while Google was being asked.
            already = await self._granted_before(token, user_id)
            if already is not None:
                return already
            raise ExternalServiceError(_UNAVAILABLE)
        logger.info(
            "Prime access granted by Google Play: user=%s token=%s order=%s app_order=%s test=%s",
            user_id,
            token_tail(token),
            purchase.order_id,
            _log_value(payload.order_id),
            purchase.purchase_type == 0,
        )
        if purchase.acknowledgement_state != _ACKNOWLEDGED:
            await self._acknowledge(product_id, token)
        return await self._access.get_access(user_id)

    async def _granted_before(self, token: str, user_id: uuid.UUID) -> AccessResponse | None:
        """The answer for a token already granted: its owner gets 200 again, since the app asks
        again when its consume step failed, and anyone else gets 409. None if it is new."""
        record = await self._store.find_play_purchase(token)
        if record is None:
            return None
        if record.user_id != user_id:
            logger.warning("Google Play token already granted to another account: user=%s token=%s", user_id, token_tail(token))
            raise ConflictError(_ALREADY_USED)
        if not record.acknowledged:
            await self._acknowledge(self._settings.assistant_google_play_product_id, token)
        return await self._access.get_access(user_id)

    async def _read_purchase(self, product_id: str, token: str, user_id: uuid.UUID) -> PlayPurchase | None:
        """Google's word on the purchase, checked. None while it is still pending."""
        try:
            async with asyncio.timeout(_GOOGLE_SECONDS):
                purchase = await self._client.get_purchase(product_id, token)
        except PlayPurchaseNotFoundError as exc:
            logger.warning("Google Play does not know this purchase: user=%s token=%s %s", user_id, token_tail(token), exc)
            raise BadRequestError(_NOT_VERIFIED) from exc
        except (GooglePlayUnavailableError, TimeoutError) as exc:
            logger.warning("Google Play could not be asked about a purchase: user=%s token=%s %s", user_id, token_tail(token), type(exc).__name__)
            raise ExternalServiceError(_UNAVAILABLE) from exc
        state = purchase.purchase_state
        if state == _PENDING:
            logger.info("Google Play purchase still pending: user=%s token=%s", user_id, token_tail(token))
            return None
        if state != _PURCHASED:
            logger.warning("Google Play purchase not completed: user=%s token=%s state=%s", user_id, token_tail(token), state)
            raise BadRequestError(_NOT_VERIFIED)
        # A 403, as the spec requires, and never acknowledged: a purchase that can't be tied to
        # this account is left for Google to refund instead of being kept with nothing granted.
        if not _same_account(purchase.account_id, user_id):
            logger.warning(
                "Google Play purchase made for another account: user=%s token=%s order=%s", user_id, token_tail(token), purchase.order_id
            )
            raise ForbiddenError(_OTHER_ACCOUNT)
        return purchase

    async def _acknowledge(self, product_id: str, token: str) -> None:
        """Google refunds a purchase nobody acknowledges within 3 days. The app's consume step
        acknowledges it too, so a failure here is only logged: the grant already stands."""
        try:
            async with asyncio.timeout(_ACKNOWLEDGE_SECONDS):
                await self._client.acknowledge(product_id, token)
            await self._store.mark_play_acknowledged(token)
        except PlayAcknowledgeRefusedError:
            await self._record_if_settled(product_id, token)
        except (GooglePlayUnavailableError, TimeoutError, DatabaseError) as exc:
            logger.warning("Google Play purchase not acknowledged yet: token=%s %s %s", token_tail(token), type(exc).__name__, exc)

    async def _record_if_settled(self, product_id: str, token: str) -> None:
        """Google refuses to acknowledge a purchase the app has already acknowledged or consumed.
        Asking which, once, means a settled purchase is recorded and never retried again."""
        try:
            async with asyncio.timeout(_ACKNOWLEDGE_SECONDS):
                purchase = await self._client.get_purchase(product_id, token)
            if purchase.acknowledgement_state == _ACKNOWLEDGED or purchase.consumption_state == _CONSUMED:
                await self._store.mark_play_acknowledged(token)
                return
            logger.warning("Google Play refused to acknowledge an unsettled purchase: token=%s", token_tail(token))
        except (GooglePlayUnavailableError, PlayPurchaseNotFoundError, TimeoutError, DatabaseError) as exc:
            logger.warning("Google Play purchase not acknowledged yet: token=%s %s", token_tail(token), type(exc).__name__)
