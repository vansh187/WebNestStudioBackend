"""WebNest Prime Member: one Razorpay payment buys 30 days and 2M tokens. The browser can't
be trusted, so only a signature checked here, or a payment fetched from Razorpay, grants
anything. Never logs secrets, signatures or webhook bodies, which can hold customer details."""

import hashlib
import hmac
import json
import logging
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

from core.config import Settings
from core.exceptions import (
    BadRequestError,
    ConflictError,
    ExternalServiceError,
    NotFoundError,
    PaymentRequiredError,
    RateLimitedError,
)
from database.assistant_payment_persistence import AccessState, AssistantPaymentStore, NewOrder, WebhookEvent
from schemas.assistant_schemas import AccessResponse, MembershipResponse, OrderResponse, VerifyRequest
from services.razorpay_client import RazorpayClient, RazorpayError

logger = logging.getLogger("webnest.assistant.payments")

PRIME_LABEL = "WebNest Prime Member"
CURRENCY = "INR"
ORDERS_PER_MINUTE = 5
_ORDER_WINDOW_SECONDS = 60.0
_MAX_TRACKED_USERS = 10_000
_RECONCILE_WINDOW = timedelta(hours=24)
_RECONCILE_MAX_ORDERS = 200
_LOG_ID_CHARS = 64
_UNAVAILABLE = "Payments are not available right now. Please try again later."


class InvalidWebhookSignatureError(Exception):
    pass


class WebhookNotConfiguredError(Exception):
    pass


class OrderRateLimiter:
    """Orders per user per minute, in process memory. On the event loop only, so no lock."""

    def __init__(self) -> None:
        self._orders: dict[uuid.UUID, deque[float]] = {}
        self._creating: set[uuid.UUID] = set()

    def begin_order(self, user_id: uuid.UUID) -> bool:
        """False while this user already has an order being created, so a double-click on
        Pay can't open two orders that could then both be paid."""
        if user_id in self._creating:
            return False
        self._creating.add(user_id)
        return True

    def end_order(self, user_id: uuid.UUID) -> None:
        self._creating.discard(user_id)

    def allow(self, user_id: uuid.UUID, now: float) -> bool:
        cutoff = now - _ORDER_WINDOW_SECONDS
        if len(self._orders) >= _MAX_TRACKED_USERS:
            self._orders = {user: times for user, times in self._orders.items() if times and times[-1] > cutoff}
        times = self._orders.setdefault(user_id, deque())
        while times and times[0] <= cutoff:
            times.popleft()
        if len(times) >= ORDERS_PER_MINUTE:
            return False
        times.append(now)
        return True


def _hex_hmac(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _same(expected: str, given: str) -> bool:
    # As bytes: compare_digest raises on a str holding anything beyond ASCII.
    return hmac.compare_digest(expected.encode("utf-8"), given.encode("utf-8", "replace"))


def _safe_id(value: str) -> str:
    """An id for the logs, from a header nothing has vouched for yet."""
    return repr(value[:_LOG_ID_CHARS])


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_str(value: Any) -> str | None:
    # A NUL byte can't be stored in Postgres, so it would fail every retry of the event.
    return value if isinstance(value, str) and value and "\x00" not in value else None


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _entity(payload: dict[str, Any], name: str) -> dict[str, Any]:
    return _as_dict(_as_dict(payload.get(name)).get("entity"))


def parse_webhook(event_id: str, body: Any) -> WebhookEvent:
    """Only the fields we act on, from the shapes in Razorpay's webhook samples."""
    root = _as_dict(body)
    payload = _as_dict(root.get("payload"))
    event_type = _as_str(root.get("event")) or "unknown"
    payment = _entity(payload, "payment")
    if event_type == "order.paid":
        order = _entity(payload, "order")
        return WebhookEvent(
            event_id=event_id,
            event_type=event_type,
            order_id=_as_str(order.get("id")),
            payment_id=_as_str(payment.get("id")),
            amount=_as_int(order.get("amount")),
            currency=_as_str(order.get("currency")),
        )
    amount = _as_int(payment.get("amount"))
    refunded = _as_int(payment.get("amount_refunded"))
    return WebhookEvent(
        event_id=event_id,
        event_type=event_type,
        order_id=_as_str(payment.get("order_id")),
        payment_id=_as_str(payment.get("id")),
        amount=amount,
        currency=_as_str(payment.get("currency")),
        full_refund=payment.get("refund_status") == "full" or (amount is not None and refunded is not None and refunded >= amount > 0),
    )


class AssistantPaymentService:
    def __init__(self, settings: Settings, store: AssistantPaymentStore, razorpay: RazorpayClient, limiter: OrderRateLimiter) -> None:
        self._settings = settings
        self._store = store
        self._razorpay = razorpay
        self._limiter = limiter

    @property
    def paywall_enabled(self) -> bool:
        return self._settings.assistant_paywall_enabled

    def access_response(self, state: AccessState | None) -> AccessResponse:
        now = datetime.now(timezone.utc)
        running = state is not None and state.expires_at > now
        return AccessResponse(
            active=running and state is not None and state.tokens_used < state.tokens_limit,
            expires_at=state.expires_at if state is not None else None,
            price_paise=self._settings.assistant_price_paise,
            pass_days=self._settings.assistant_pass_days,
            tokens_limit=state.tokens_limit if state is not None else self._settings.assistant_pass_tokens,
            tokens_used=state.tokens_used if state is not None else 0,
            is_prime=running,
            member_label=PRIME_LABEL if running else None,
        )

    async def get_access(self, user_id: uuid.UUID) -> AccessResponse:
        return self.access_response(await self._store.get_access(user_id))

    async def get_membership(self, user_id: uuid.UUID) -> MembershipResponse:
        """The badge. A member who has used all their tokens is still a member until the
        pass ends: `is_prime` is about the paid period, `access.active` about using the AI."""
        access = await self.get_access(user_id)
        return MembershipResponse(is_prime=access.is_prime, member_label=access.member_label, expires_at=access.expires_at if access.is_prime else None)

    async def ensure_access(self, user_id: uuid.UUID) -> None:
        """The real lock. It runs before the AI provider is contacted, and a database
        failure here refuses the call rather than giving it away."""
        if not (await self.get_access(user_id)).active:
            raise PaymentRequiredError("Unlock WebNest AI to continue.")

    async def add_tokens(self, user_id: uuid.UUID, tokens: int) -> AccessResponse | None:
        """The access after this call's tokens, or None if the user has no pass to charge."""
        state = await self._store.add_tokens(user_id, tokens)
        return None if state is None else self.access_response(state)

    async def create_order(self, user_id: uuid.UUID) -> OrderResponse:
        key_id = self._settings.assistant_razorpay_key_id
        if not self.paywall_enabled or not key_id or not self._settings.assistant_razorpay_key_secret:
            raise ExternalServiceError(_UNAVAILABLE)
        if not self._limiter.begin_order(user_id):
            raise RateLimitedError("Your payment is already being started. Please wait a moment.")
        try:
            return await self._create_order(user_id, key_id)
        finally:
            self._limiter.end_order(user_id)

    async def _create_order(self, user_id: uuid.UUID, key_id: str) -> OrderResponse:
        if (await self.get_access(user_id)).active:
            raise ConflictError("You already have active WebNest Prime access.")
        if not self._limiter.allow(user_id, time.monotonic()):
            raise RateLimitedError("Too many payment attempts. Please wait a minute and try again.")
        amount = self._settings.assistant_price_paise
        days = self._settings.assistant_pass_days
        try:
            order_id = await self._razorpay.create_order(amount, CURRENCY, f"wn_{uuid.uuid4().hex}", {"user_id": str(user_id)})
        except RazorpayError as exc:
            raise ExternalServiceError(_UNAVAILABLE) from exc
        await self._store.save_order(
            NewOrder(
                user_id=user_id,
                order_id=order_id,
                amount_paise=amount,
                currency=CURRENCY,
                pass_days=days,
                pass_tokens=self._settings.assistant_pass_tokens,
            )
        )
        logger.info("Razorpay order created: order=%s user=%s", order_id, user_id)
        return OrderResponse(
            order_id=order_id,
            amount=amount,
            currency=CURRENCY,
            key_id=key_id,
            name="WebNest Studio",
            description=f"{PRIME_LABEL}, {days} days",
        )

    async def verify(self, user_id: uuid.UUID, payload: VerifyRequest) -> AccessResponse:
        secret = self._settings.assistant_razorpay_key_secret
        if not secret:
            raise ExternalServiceError(_UNAVAILABLE)
        order = await self._store.find_user_order(payload.razorpay_order_id, user_id)
        if order is None:
            logger.warning("Razorpay checkout verify: order not found for user: order=%s user=%s", payload.razorpay_order_id, user_id)
            raise NotFoundError("Order not found.")
        # The order id from our database, not the one the browser sent.
        message = f"{order.order_id}|{payload.razorpay_payment_id}".encode("utf-8")
        if not _same(_hex_hmac(secret, message), payload.razorpay_signature):
            logger.warning("Razorpay checkout signature mismatch: order=%s payment=%s user=%s", order.order_id, payload.razorpay_payment_id, user_id)
            raise BadRequestError("This payment could not be verified.")
        logger.info("Razorpay checkout signature verified: order=%s payment=%s user=%s", order.order_id, payload.razorpay_payment_id, user_id)
        granted = await self._store.grant(order.order_id, payload.razorpay_payment_id)
        logger.info(
            "Razorpay checkout verify finished: order=%s payment=%s user=%s access=%s",
            order.order_id,
            payload.razorpay_payment_id,
            user_id,
            "granted" if granted else "already_granted",
        )
        return await self.get_access(user_id)

    def _webhook_signature_ok(self, raw_body: bytes, signature: str) -> bool:
        # The previous secret still counts during a rotation, for events already being retried.
        secrets = (self._settings.assistant_razorpay_webhook_secret, self._settings.assistant_razorpay_webhook_secret_previous)
        return any(_same(_hex_hmac(secret, raw_body), signature) for secret in secrets if secret)

    async def handle_webhook(self, raw_body: bytes, signature: str, event_id: str) -> None:
        """Signed over the raw bytes, before any parsing. A failed database write raises, so
        Razorpay retries. Anything else gets a 200, so odd events can't disable the webhook."""
        if not self._settings.assistant_razorpay_webhook_secret:
            logger.error("Razorpay webhook received but ASSISTANT_RAZORPAY_WEBHOOK_SECRET is not set")
            raise WebhookNotConfiguredError
        if not self._webhook_signature_ok(raw_body, signature):
            logger.warning("Razorpay webhook signature mismatch: event=%s", _safe_id(event_id))
            raise InvalidWebhookSignatureError
        logger.info("Razorpay webhook signature verified: event=%s", _safe_id(event_id))
        # Razorpay always sends an id; the hash means a missing one still can't count twice.
        event_id = event_id or hashlib.sha256(raw_body).hexdigest()
        try:
            body = json.loads(raw_body)
        except (ValueError, RecursionError):
            logger.error("Razorpay webhook was signed but is not usable JSON: event=%s", _safe_id(event_id))
            return
        event = parse_webhook(event_id, body)
        outcome = await self._store.apply_webhook(event)
        level = logging.ERROR if outcome in ("mismatch", "malformed") else logging.WARNING if outcome == "unknown_order" else logging.INFO
        logger.log(
            level,
            "Razorpay webhook: event=%s type=%s order=%s payment=%s outcome=%s",
            _safe_id(event_id),
            event.event_type[:_LOG_ID_CHARS],
            event.order_id,
            event.payment_id,
            outcome,
        )

    async def reconcile(self) -> int:
        """Recovers a user if the webhook was down: asks Razorpay about recent unpaid orders
        and grants through the same function for any captured payment. One order's failure
        doesn't stop the rest, since the next run tries again."""
        granted = 0
        since = datetime.now(timezone.utc) - _RECONCILE_WINDOW
        for order in await self._store.open_orders(since, _RECONCILE_MAX_ORDERS):
            try:
                payments = await self._razorpay.order_payments(order.order_id)
            except RazorpayError:
                continue
            for payment in payments:
                if payment.status != "captured" or not payment.payment_id:
                    continue
                if payment.amount != order.amount_paise or payment.currency != order.currency:
                    logger.error("Razorpay payment does not match its order: order=%s payment=%s", order.order_id, payment.payment_id)
                    continue
                if await self._store.grant(order.order_id, payment.payment_id):
                    granted += 1
                    logger.info("Prime access granted by reconciliation: order=%s payment=%s", order.order_id, payment.payment_id)
                break
        return granted
