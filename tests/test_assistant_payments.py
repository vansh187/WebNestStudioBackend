"""WebNest Prime Member: orders, verify, the webhook's signature, the 402 lock and token
counting. Offline: a fake Razorpay and an in-memory store, so no real calls and no database.
What only a real transaction can prove is in test_assistant_payments_sql.py."""

import asyncio
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.assistant_payment_router import router as payment_router
from api.assistant_payment_router import webhook_router
from api.assistant_router import get_assistant_user_id
from api.assistant_router import router as assistant_router
from core.config import Settings
from core.dependencies import get_assistant_payment_service, get_assistant_service
from core.error_handlers import register_error_handlers
from core.exceptions import DatabaseError, ExternalServiceError, RateLimitedError
from database.assistant_payment_persistence import AccessState, NewOrder, SavedOrder, WebhookEvent, WebhookOutcome
from services.assistant_llm import FinishedEvent, ProviderStreamError, StreamOpened, TextDelta, UsageEvent
from services.assistant_payment_jobs import register_payment_jobs, reconcile_payments
from services.assistant_payment_service import PRIME_LABEL, AssistantPaymentService, OrderRateLimiter, parse_webhook
from services.razorpay_client import HttpRazorpayClient, RazorpayError, RazorpayPayment
from tests.test_assistant import BODY, FakeProvider, FakeStore, make_service, parse_sse

USER = uuid.uuid4()
OTHER_USER = uuid.uuid4()
KEY_SECRET = "test-key-secret"
WEBHOOK_SECRET = "test-webhook-secret"
LATER = datetime.now(timezone.utc) + timedelta(days=10)
EARLIER = datetime.now(timezone.utc) - timedelta(days=1)


def settings(**changes: Any) -> SimpleNamespace:
    values = {
        "assistant_enabled": True,
        "assistant_provider": "groq",
        "assistant_groq_api_key": "placeholder",
        "assistant_anthropic_api_key": "",
        "assistant_paywall_enabled": True,
        "assistant_price_paise": 29900,
        "assistant_pass_days": 30,
        "assistant_pass_tokens": 2_000_000,
        "assistant_pass_daily_tokens": 150_000,
        "assistant_pass_weekly_tokens": 700_000,
        "assistant_razorpay_key_id": "rzp_test_key",
        "assistant_razorpay_key_secret": KEY_SECRET,
        "assistant_razorpay_webhook_secret": WEBHOOK_SECRET,
        "assistant_razorpay_webhook_secret_previous": "",
    }
    return SimpleNamespace(**{**values, **changes})


class FakeRazorpay:
    def __init__(self) -> None:
        self.orders: list[dict[str, Any]] = []
        self.fail = False
        self.payments: dict[str, list[RazorpayPayment]] = {}
        self.one_payment = RazorpayPayment("pay_1", "captured", 29900, "INR", "order_1")

    async def create_order(self, amount: int, currency: str, receipt: str, notes: dict[str, str]) -> str:
        if self.fail:
            raise RazorpayError("down")
        self.orders.append({"amount": amount, "currency": currency, "receipt": receipt, "notes": notes})
        return f"order_{len(self.orders)}"

    async def order_payments(self, order_id: str) -> list[RazorpayPayment]:
        if self.fail:
            raise RazorpayError("down")
        return self.payments.get(order_id, [])

    async def get_payment(self, payment_id: str) -> RazorpayPayment:
        if self.fail:
            raise RazorpayError("down")
        return self.one_payment

    async def aclose(self) -> None:
        pass


class FakePaymentStore:
    def __init__(self, access: AccessState | None = None) -> None:
        self.access: dict[uuid.UUID, AccessState] = {USER: access} if access else {}
        self.orders: dict[str, NewOrder] = {}
        self.statuses: dict[str, str] = {}
        self.grants: list[tuple[str, str]] = []
        self.events: list[WebhookEvent] = []
        self.charged: list[int] = []
        self.fail_charge = False
        self.reserved = 0
        self.released: list[int] = []

    async def get_access(self, user_id: uuid.UUID) -> AccessState | None:
        return self.access.get(user_id)

    async def save_order(self, order: NewOrder) -> None:
        self.orders[order.order_id] = order
        self.statuses[order.order_id] = "created"

    def _saved(self, order: NewOrder) -> SavedOrder:
        return SavedOrder(order.order_id, order.user_id, order.amount_paise, order.currency, self.statuses[order.order_id])

    async def find_user_order(self, order_id: str, user_id: uuid.UUID) -> SavedOrder | None:
        order = self.orders.get(order_id)
        return self._saved(order) if order is not None and order.user_id == user_id else None

    async def grant(self, order_id: str, payment_id: str) -> bool:
        if self.statuses.get(order_id) in ("paid", "refunded"):
            return False
        self.statuses[order_id] = "paid"
        self.grants.append((order_id, payment_id))
        order = self.orders[order_id]
        self.access[order.user_id] = AccessState(datetime.now(timezone.utc) + timedelta(days=order.pass_days), order.pass_tokens, 0)
        return True

    async def reserve_tokens(self, user_id: uuid.UUID, tokens: int) -> bool:
        state = self.access.get(user_id)
        if state is None or state.expires_at <= datetime.now(timezone.utc) or state.tokens_used >= state.tokens_limit:
            return False
        self.reserved = tokens
        self.access[user_id] = AccessState(state.expires_at, state.tokens_limit, state.tokens_used + tokens)
        return True

    async def add_tokens(self, user_id: uuid.UUID, tokens: int) -> AccessState | None:
        if self.fail_charge:
            raise RuntimeError("database down")
        # What the call really spent: its change on top of what it reserved.
        if tokens + self.reserved == 0:
            self.released.append(self.reserved)
        self.charged.append(tokens + self.reserved)
        state = self.access.get(user_id)
        if state is None:
            return None
        self.access[user_id] = AccessState(state.expires_at, state.tokens_limit, state.tokens_used + tokens)
        return self.access[user_id]

    async def apply_webhook(self, event: WebhookEvent) -> WebhookOutcome:
        self.events.append(event)
        return "ignored"

    async def open_orders(self, since: datetime, limit: int, per_user: int) -> list[SavedOrder]:
        return [self._saved(order) for order in self.orders.values() if self.statuses[order.order_id] in ("created", "failed")]


def payment_service(store: FakePaymentStore | None = None, razorpay: FakeRazorpay | None = None, **changes: Any) -> AssistantPaymentService:
    return AssistantPaymentService(settings(**changes), store or FakePaymentStore(), razorpay or FakeRazorpay(), OrderRateLimiter())  # type: ignore[arg-type]


def client_for(payments: AssistantPaymentService, assistant: Any = None, user: uuid.UUID | None = USER) -> TestClient:
    app = FastAPI()
    app.include_router(assistant_router)
    app.include_router(payment_router)
    app.include_router(webhook_router)
    register_error_handlers(app)
    app.dependency_overrides[get_assistant_payment_service] = lambda: payments
    if user is not None:
        app.dependency_overrides[get_assistant_user_id] = lambda: user
    if assistant is not None:
        app.dependency_overrides[get_assistant_service] = lambda: assistant
    return TestClient(app, raise_server_exceptions=False)


def checkout_signature(order_id: str, payment_id: str, secret: str = KEY_SECRET) -> str:
    return hmac.new(secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()


def webhook_signature(raw: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def post_webhook(client: TestClient, raw: bytes, signature: str | None = None, event_id: str = "evt_1") -> httpx.Response:
    headers = {"x-razorpay-signature": webhook_signature(raw) if signature is None else signature, "x-razorpay-event-id": event_id}
    return client.post("/api/webhooks/razorpay", content=raw, headers=headers)


def captured_body(order_id: str = "order_1", amount: int = 29900) -> dict[str, Any]:
    return {
        "event": "payment.captured",
        "payload": {"payment": {"entity": {"id": "pay_1", "order_id": order_id, "amount": amount, "currency": "INR", "status": "captured", "email": "customer@example.com"}}},
    }


def paid_service(store: FakePaymentStore, provider: FakeProvider | None = None, usage_store: FakeStore | None = None, **changes: Any) -> tuple[Any, AssistantPaymentService]:
    payments = payment_service(store, **changes)
    assistant, _, _, _ = make_service(provider, usage_store)
    assistant._payments = payments
    return assistant, payments


# ---- orders -----------------------------------------------------------------------------


def test_every_payment_endpoint_requires_login() -> None:
    client = client_for(payment_service(), user=None)
    with_tokenless = [("post", "/api/assistant/payments/order"), ("post", "/api/assistant/payments/verify"), ("get", "/api/assistant/membership")]
    for method, url in with_tokenless:
        assert getattr(client, method)(url).status_code == 401, url


def test_an_order_uses_the_servers_price_whatever_the_client_sends() -> None:
    razorpay = FakeRazorpay()
    store = FakePaymentStore()
    response = client_for(payment_service(store, razorpay)).post("/api/assistant/payments/order", json={"amount": 1, "currency": "USD", "user_id": str(OTHER_USER)})
    assert response.status_code == 200
    assert response.json() == {
        "order_id": "order_1",
        "amount": 29900,
        "currency": "INR",
        "key_id": "rzp_test_key",
        "name": "WebNest Studio",
        "description": "WebNest Prime Member, 30 days",
    }
    sent = razorpay.orders[0]
    assert (sent["amount"], sent["currency"], sent["notes"]) == (29900, "INR", {"user_id": str(USER)})
    assert len(sent["receipt"]) <= 40
    saved = store.orders["order_1"]
    assert (saved.user_id, saved.amount_paise, saved.pass_days, saved.pass_tokens) == (USER, 29900, 30, 2_000_000)


def test_each_call_makes_a_new_order_with_a_new_receipt() -> None:
    razorpay = FakeRazorpay()
    client = client_for(payment_service(razorpay=razorpay))
    ids = {client.post("/api/assistant/payments/order").json()["order_id"] for _ in range(3)}
    assert len(ids) == 3
    assert len({order["receipt"] for order in razorpay.orders}) == 3


def test_an_active_member_gets_409_and_no_order() -> None:
    razorpay = FakeRazorpay()
    store = FakePaymentStore(AccessState(LATER, 100, 10))
    assert client_for(payment_service(store, razorpay)).post("/api/assistant/payments/order").status_code == 409
    assert razorpay.orders == []


@pytest.mark.parametrize("state", [AccessState(LATER, 100, 100), AccessState(EARLIER, 100, 0)], ids=["out_of_tokens", "expired"])
def test_a_user_with_no_usable_pass_can_buy_again(state: AccessState) -> None:
    assert client_for(payment_service(FakePaymentStore(state))).post("/api/assistant/payments/order").status_code == 200


def test_more_than_five_orders_a_minute_gets_429() -> None:
    razorpay = FakeRazorpay()
    client = client_for(payment_service(razorpay=razorpay))
    assert [client.post("/api/assistant/payments/order").status_code for _ in range(6)] == [200] * 5 + [429]
    assert len(razorpay.orders) == 5


def test_the_order_limit_clears_after_a_minute_and_is_per_user() -> None:
    limiter = OrderRateLimiter()
    assert all(limiter.allow(USER, 0.0) for _ in range(5))
    assert not limiter.allow(USER, 1.0)
    assert limiter.allow(OTHER_USER, 1.0)
    assert limiter.allow(USER, 61.0)


def test_a_double_click_on_pay_creates_one_order() -> None:
    class SlowRazorpay(FakeRazorpay):
        async def create_order(self, amount: int, currency: str, receipt: str, notes: dict[str, str]) -> str:
            await asyncio.sleep(0.01)
            return await super().create_order(amount, currency, receipt, notes)

    async def main() -> None:
        razorpay = SlowRazorpay()
        service = payment_service(razorpay=razorpay)
        results = await asyncio.gather(service.create_order(USER), service.create_order(USER), return_exceptions=True)
        assert len(razorpay.orders) == 1
        assert sum(isinstance(result, RateLimitedError) for result in results) == 1
        # The guard ends with the request, so a later attempt works.
        assert (await service.create_order(USER)).order_id == "order_2"

    asyncio.run(main())


def test_the_double_click_guard_is_released_after_a_failure() -> None:
    async def main() -> None:
        razorpay = FakeRazorpay()
        razorpay.fail = True
        service = payment_service(razorpay=razorpay)
        with pytest.raises(ExternalServiceError):
            await service.create_order(USER)
        razorpay.fail = False
        assert (await service.create_order(USER)).order_id == "order_1"

    asyncio.run(main())


def test_the_double_click_guard_does_not_block_other_users() -> None:
    limiter = OrderRateLimiter()
    assert limiter.begin_order(USER)
    assert not limiter.begin_order(USER)
    assert limiter.begin_order(OTHER_USER)
    limiter.end_order(USER)
    assert limiter.begin_order(USER)


@pytest.mark.parametrize(
    "changes",
    [{"assistant_paywall_enabled": False}, {"assistant_razorpay_key_id": ""}, {"assistant_razorpay_key_secret": ""}],
    ids=["paywall_off", "no_key_id", "no_secret"],
)
def test_order_is_503_when_payments_are_not_set_up(changes: dict[str, Any]) -> None:
    razorpay = FakeRazorpay()
    assert client_for(payment_service(razorpay=razorpay, **changes)).post("/api/assistant/payments/order").status_code == 503
    assert razorpay.orders == []


def test_a_razorpay_failure_is_503_with_our_own_message() -> None:
    razorpay = FakeRazorpay()
    razorpay.fail = True
    store = FakePaymentStore()
    response = client_for(payment_service(store, razorpay)).post("/api/assistant/payments/order")
    assert response.status_code == 503
    assert response.json() == {"detail": "Payments are not available right now. Please try again later."}
    assert store.orders == {}


# ---- verify -----------------------------------------------------------------------------


def verify_client(store: FakePaymentStore, user: uuid.UUID = USER, razorpay: FakeRazorpay | None = None) -> TestClient:
    asyncio.run(store.save_order(NewOrder(USER, "order_1", 29900, "INR", 30, 2_000_000)))
    return client_for(payment_service(store, razorpay), user=user)


def verify_body(order_id: str = "order_1", payment_id: str = "pay_1", signature: str | None = None) -> dict[str, str]:
    return {
        "razorpay_order_id": order_id,
        "razorpay_payment_id": payment_id,
        "razorpay_signature": signature if signature is not None else checkout_signature(order_id, payment_id),
    }


def test_a_correct_signature_grants_access_and_makes_a_prime_member() -> None:
    store = FakePaymentStore()
    response = verify_client(store).post("/api/assistant/payments/verify", json=verify_body())
    assert response.status_code == 200
    access = response.json()["access"]
    assert access["active"] is True
    assert access["is_prime"] is True
    assert access["member_label"] == "WebNest Prime Member"
    assert (access["tokens_limit"], access["tokens_used"], access["price_paise"], access["pass_days"]) == (2_000_000, 0, 29900, 30)
    assert store.grants == [("order_1", "pay_1")]


def test_a_wrong_signature_gives_400_and_grants_nothing() -> None:
    store = FakePaymentStore()
    response = verify_client(store).post("/api/assistant/payments/verify", json=verify_body(signature="0" * 64))
    assert response.status_code == 400
    assert store.grants == []


def test_a_non_ascii_signature_is_a_clean_400() -> None:
    store = FakePaymentStore()
    response = verify_client(store).post("/api/assistant/payments/verify", json=verify_body(signature="é" * 64))
    assert response.status_code == 400


def test_a_signature_made_with_another_secret_is_refused() -> None:
    store = FakePaymentStore()
    response = verify_client(store).post("/api/assistant/payments/verify", json=verify_body(signature=checkout_signature("order_1", "pay_1", "other")))
    assert response.status_code == 400
    assert store.grants == []


def test_another_users_order_gives_404() -> None:
    store = FakePaymentStore()
    assert verify_client(store, OTHER_USER).post("/api/assistant/payments/verify", json=verify_body()).status_code == 404
    assert store.grants == []


def test_the_signature_is_checked_against_the_stored_order_id() -> None:
    # Signed for an order that isn't this one: a browser can't move a payment between orders.
    store = FakePaymentStore()
    body = verify_body(signature=checkout_signature("order_other", "pay_1"))
    assert verify_client(store).post("/api/assistant/payments/verify", json=body).status_code == 400


def test_sending_verify_twice_gives_one_grant_and_two_200s() -> None:
    store = FakePaymentStore()
    client = verify_client(store)
    assert [client.post("/api/assistant/payments/verify", json=verify_body()).status_code for _ in range(2)] == [200, 200]
    assert len(store.grants) == 1


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not json",
        b"{}",
        b'{"razorpay_order_id": "order_1"}',
        b'{"razorpay_order_id": "", "razorpay_payment_id": "p", "razorpay_signature": "s"}',
        b'{"razorpay_order_id": "order\\u00001", "razorpay_payment_id": "p", "razorpay_signature": "s"}',
        b'{"razorpay_order_id": "order_1", "razorpay_payment_id": "p\\u0000", "razorpay_signature": "s"}',
        b"[" * 4000,
    ],
    ids=["empty", "not_json", "no_fields", "missing_fields", "blank_field", "nul_in_order_id", "nul_in_payment_id", "deep_json"],
)
def test_a_bad_verify_body_gives_400_without_echoing_it(raw: bytes) -> None:
    response = verify_client(FakePaymentStore()).post("/api/assistant/payments/verify", content=raw)
    assert response.status_code == 400
    assert response.json() == {"detail": "This payment could not be verified."}


@pytest.mark.parametrize(
    "payment",
    [
        RazorpayPayment("pay_1", "authorized", 29900, "INR", "order_1"),
        RazorpayPayment("pay_1", "failed", 29900, "INR", "order_1"),
        RazorpayPayment("pay_1", "captured", 29900, "INR", "order_other"),
        RazorpayPayment("pay_1", "captured", 1, "INR", "order_1"),
        RazorpayPayment("pay_1", "captured", 29900, "USD", "order_1"),
        RazorpayPayment("pay_1", "captured", 29900, "INR", None),
    ],
    ids=["only_authorized", "failed", "another_order", "wrong_amount", "wrong_currency", "no_order"],
)
def test_a_valid_signature_alone_grants_nothing_until_the_money_is_captured(payment: RazorpayPayment) -> None:
    store = FakePaymentStore()
    razorpay = FakeRazorpay()
    razorpay.one_payment = payment
    response = verify_client(store, razorpay=razorpay).post("/api/assistant/payments/verify", json=verify_body())
    assert response.status_code == 200, "the panel keeps reading usage"
    assert response.json()["access"]["active"] is False
    assert store.grants == []


def test_razorpay_being_down_during_verify_grants_nothing_and_is_not_an_error() -> None:
    store = FakePaymentStore()
    razorpay = FakeRazorpay()
    razorpay.fail = True
    assert verify_client(store, razorpay=razorpay).post("/api/assistant/payments/verify", json=verify_body()).status_code == 200
    assert store.grants == []


@pytest.mark.parametrize(
    "changes",
    [
        {"assistant_enabled": False},
        {"assistant_groq_api_key": ""},
        {"assistant_provider": "other"},
    ],
    ids=["assistant_off", "no_provider_key", "unknown_provider"],
)
def test_nobody_can_pay_while_the_assistant_can_not_answer(changes: dict[str, Any]) -> None:
    razorpay = FakeRazorpay()
    assert client_for(payment_service(razorpay=razorpay, **changes)).post("/api/assistant/payments/order").status_code == 503
    assert razorpay.orders == []


def test_claude_alone_is_enough_to_sell_a_pass() -> None:
    service = payment_service(assistant_provider="anthropic", assistant_groq_api_key="", assistant_anthropic_api_key="key")
    assert client_for(service).post("/api/assistant/payments/order").status_code == 200


# ---- the webhook's signature ------------------------------------------------------------


def test_a_wrong_webhook_signature_gives_400_and_does_nothing() -> None:
    store = FakePaymentStore()
    raw = json.dumps(captured_body()).encode()
    assert post_webhook(client_for(payment_service(store), user=None), raw, "0" * 64).status_code == 400
    assert store.events == []


def test_a_missing_webhook_signature_gives_400() -> None:
    store = FakePaymentStore()
    response = client_for(payment_service(store), user=None).post("/api/webhooks/razorpay", content=b"{}")
    assert response.status_code == 400
    assert store.events == []


def test_the_signature_covers_the_raw_bytes_not_parsed_json() -> None:
    store = FakePaymentStore()
    client = client_for(payment_service(store), user=None)
    spaced = b'{ "event" :   "payment.captured",\n "payload": {} }'
    assert post_webhook(client, spaced).status_code == 200
    # The same JSON re-encoded is different bytes, so a signature for it doesn't fit.
    assert post_webhook(client, spaced, webhook_signature(json.dumps(json.loads(spaced)).encode())).status_code == 400
    assert len(store.events) == 1


def test_the_webhook_needs_no_login() -> None:
    assert post_webhook(client_for(payment_service(), user=None), b"{}").status_code == 200


def test_the_previous_secret_still_works_during_a_rotation() -> None:
    client = client_for(payment_service(assistant_razorpay_webhook_secret_previous="old-secret"), user=None)
    assert post_webhook(client, b"{}", webhook_signature(b"{}", "old-secret")).status_code == 200
    assert post_webhook(client, b"{}", webhook_signature(b"{}", "wrong")).status_code == 400


def test_an_empty_previous_secret_never_matches() -> None:
    client = client_for(payment_service(), user=None)
    assert post_webhook(client, b"{}", webhook_signature(b"{}", "")).status_code == 400


def test_with_no_webhook_secret_nothing_is_accepted() -> None:
    store = FakePaymentStore()
    client = client_for(payment_service(store, assistant_razorpay_webhook_secret=""), user=None)
    assert post_webhook(client, b"{}", webhook_signature(b"{}", "")).status_code == 503
    assert store.events == []


def test_a_signed_body_that_is_not_json_gets_200_so_the_webhook_stays_enabled() -> None:
    store = FakePaymentStore()
    assert post_webhook(client_for(payment_service(store), user=None), b"not json").status_code == 200
    assert store.events == []


@pytest.mark.parametrize("raw", [b"[" * 100_000, b'{"a":' * 50_000, b"\xff\xfe"], ids=["deep_array", "deep_object", "not_utf8"])
def test_a_signed_body_that_can_not_be_parsed_gets_200_and_never_raises(raw: bytes) -> None:
    store = FakePaymentStore()
    assert post_webhook(client_for(payment_service(store), user=None), raw).status_code == 200
    assert store.events == []


def test_a_nul_byte_in_a_signed_event_never_reaches_the_database() -> None:
    event = parse_webhook("e", captured_body(order_id="order\x001"))
    assert event.order_id is None
    assert parse_webhook("e", {"event": "x\x00y"}).event_type == "unknown"


def test_the_signature_logs_say_what_happened_without_the_signature(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO")
    store = FakePaymentStore()
    client = verify_client(store)
    body = verify_body()
    client.post("/api/assistant/payments/verify", json=body)
    client.post("/api/assistant/payments/verify", json=body)
    client.post("/api/assistant/payments/verify", json=verify_body(signature="0" * 64))
    raw = json.dumps(captured_body()).encode()
    signature = webhook_signature(raw)
    post_webhook(client, raw, signature)
    post_webhook(client, raw, "1" * 64, "evt_bad")
    logged = "\n".join(record.getMessage() for record in caplog.records if record.name.startswith("webnest"))
    assert f"Razorpay checkout signature verified: order=order_1 payment=pay_1 user={USER}" in logged
    assert "access=granted" in logged
    assert "access=already_granted" in logged
    assert "Razorpay checkout signature mismatch: order=order_1" in logged
    assert "Razorpay webhook signature verified: event='evt_1'" in logged
    assert "Razorpay webhook signature mismatch: event='evt_bad'" in logged
    for secret in (body["razorpay_signature"], signature, KEY_SECRET, WEBHOOK_SECRET):
        assert secret not in logged


def test_a_missing_event_id_falls_back_to_the_body_hash() -> None:
    store = FakePaymentStore()
    client = client_for(payment_service(store), user=None)
    raw = json.dumps(captured_body()).encode()
    client.post("/api/webhooks/razorpay", content=raw, headers={"x-razorpay-signature": webhook_signature(raw)})
    assert store.events[0].event_id == hashlib.sha256(raw).hexdigest()


def test_webhook_logs_hold_no_customer_details_or_signature(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO")
    raw = json.dumps(captured_body()).encode()
    signature = webhook_signature(raw)
    post_webhook(client_for(payment_service(), user=None), raw, signature)
    logged = "\n".join(record.getMessage() for record in caplog.records if record.name.startswith("webnest"))
    assert "evt_1" in logged
    assert "customer@example.com" not in logged
    assert signature not in logged
    assert WEBHOOK_SECRET not in logged


def test_no_endpoint_returns_a_secret() -> None:
    store = FakePaymentStore()
    client = verify_client(store)
    for response in (client.post("/api/assistant/payments/verify", json=verify_body()), client.get("/api/assistant/membership")):
        assert KEY_SECRET not in response.text and WEBHOOK_SECRET not in response.text


@pytest.mark.parametrize("name", ["ASSISTANT_RAZORPAY_WEBHOOK_SECRET", "RAZORPAY_WEBHOOK_SECRET", "RAZORPAY_WEBHOOK_KEY"])
def test_the_webhook_secret_is_read_from_each_accepted_name(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    for other in ("ASSISTANT_RAZORPAY_WEBHOOK_SECRET", "RAZORPAY_WEBHOOK_SECRET", "RAZORPAY_WEBHOOK_KEY"):
        monkeypatch.delenv(other, raising=False)
    monkeypatch.setenv(name, "from-this-name")
    assert Settings(_env_file=None).assistant_razorpay_webhook_secret == "from-this-name"


# ---- reading Razorpay's event shapes ----------------------------------------------------


def test_a_refund_reads_its_payment_id_from_either_entity() -> None:
    from_payment = {"event": "refund.processed", "payload": {"payment": {"entity": {"id": "pay_a", "order_id": "o"}}, "refund": {"entity": {"payment_id": "pay_b"}}}}
    assert parse_webhook("e", from_payment).payment_id == "pay_a"
    from_refund = {"event": "refund.processed", "payload": {"payment": {"entity": {"order_id": "o"}}, "refund": {"entity": {"payment_id": "pay_b"}}}}
    assert parse_webhook("e", from_refund).payment_id == "pay_b"


def test_payment_captured_is_read_from_the_payment_entity() -> None:
    event = parse_webhook("e", captured_body())
    assert (event.order_id, event.payment_id, event.amount, event.currency) == ("order_1", "pay_1", 29900, "INR")


def test_order_paid_is_read_from_the_order_entity() -> None:
    body = {
        "event": "order.paid",
        "payload": {
            "payment": {"entity": {"id": "pay_1", "order_id": "order_1", "amount": 1, "currency": "USD"}},
            "order": {"entity": {"id": "order_1", "amount": 29900, "currency": "INR"}},
        },
    }
    event = parse_webhook("e", body)
    assert (event.order_id, event.payment_id, event.amount, event.currency) == ("order_1", "pay_1", 29900, "INR")


@pytest.mark.parametrize(
    ("entity", "full"),
    [
        ({"refund_status": "full", "amount": 29900, "amount_refunded": 29900}, True),
        ({"refund_status": None, "amount": 29900, "amount_refunded": 29900}, True),
        ({"refund_status": "partial", "amount": 29900, "amount_refunded": 100}, False),
        ({"amount": 0, "amount_refunded": 0}, False),
        ({}, False),
    ],
    ids=["full", "full_by_amount", "partial", "zero", "absent"],
)
def test_only_a_full_refund_counts_as_full(entity: dict[str, Any], full: bool) -> None:
    body = {"event": "refund.processed", "payload": {"payment": {"entity": {"order_id": "order_1", **entity}}}}
    assert parse_webhook("e", body).full_refund is full


@pytest.mark.parametrize("body", [None, [], "x", 1, {}, {"payload": 1}, {"event": 5, "payload": {"payment": []}}, {"event": "order.paid", "payload": {"order": {"entity": 3}}}], ids=str)
def test_any_shape_of_signed_body_parses_without_raising(body: Any) -> None:
    assert parse_webhook("e", body).order_id is None


# ---- usage, membership and the 402 lock -------------------------------------------------


def test_usage_has_no_access_field_with_the_paywall_off() -> None:
    assistant, payments = paid_service(FakePaymentStore(), assistant_paywall_enabled=False)
    assert client_for(payments, assistant).get("/api/assistant/usage").json() == {"enabled": True, "calls_used_today": 0, "calls_limit": 40}


def test_usage_before_any_payment_shows_the_full_allowance() -> None:
    assistant, payments = paid_service(FakePaymentStore())
    body = client_for(payments, assistant).get("/api/assistant/usage").json()
    assert body["provider_name"] == "WebNest AI"
    assert body["access"] == {
        "active": False,
        "expires_at": None,
        "price_paise": 29900,
        "pass_days": 30,
        "tokens_limit": 2_000_000,
        "tokens_used": 0,
        "is_prime": False,
        "member_label": None,
    }


def test_expires_at_is_utc_iso_8601() -> None:
    expires = datetime(2026, 12, 1, tzinfo=timezone.utc)
    assistant, payments = paid_service(FakePaymentStore(AccessState(expires, 100, 1)))
    assert client_for(payments, assistant).get("/api/assistant/usage").json()["access"]["expires_at"] == "2026-12-01T00:00:00Z"


@pytest.mark.parametrize(
    ("state", "is_prime"),
    [(None, False), (AccessState(LATER, 100, 1), True), (AccessState(LATER, 100, 100), True), (AccessState(EARLIER, 100, 1), False)],
    ids=["never_paid", "active", "out_of_tokens_still_a_member", "expired"],
)
def test_membership_shows_the_badge_for_the_paid_period(state: AccessState | None, is_prime: bool) -> None:
    body = client_for(payment_service(FakePaymentStore(state))).get("/api/assistant/membership").json()
    assert body["is_prime"] is is_prime
    assert body["member_label"] == (PRIME_LABEL if is_prime else None)
    assert (body["expires_at"] is not None) is is_prime


@pytest.mark.parametrize(
    "state",
    [None, AccessState(EARLIER, 100, 0), AccessState(LATER, 100, 100), AccessState(LATER, 100, 150)],
    ids=["never_paid", "expired", "out_of_tokens", "over"],
)
def test_no_active_access_gives_402_before_any_provider_call(state: AccessState | None) -> None:
    provider = FakeProvider()
    usage_store = FakeStore()
    assistant, payments = paid_service(FakePaymentStore(state), provider, usage_store)
    response = client_for(payments, assistant).post("/api/assistant/chat", json=BODY)
    assert response.status_code == 402
    assert response.json() == {"detail": "Unlock WebNest AI to continue."}
    assert provider.requests == []
    assert usage_store.reservations == []


@pytest.mark.parametrize(("name", "model"), [("anthropic", "claude-haiku-5-5"), ("groq", "openai/gpt-oss-120b")])
def test_clients_never_see_which_provider_or_model_answered(name: str, model: str) -> None:
    provider = FakeProvider()
    provider.name = name  # type: ignore[misc]
    provider.model = model  # type: ignore[misc]
    assistant, payments = paid_service(FakePaymentStore(AccessState(LATER, 100_000, 0)), provider)
    assistant._settings.assistant_anthropic_api_key = "test-key"
    client = client_for(payments, assistant)
    seen = client.get("/api/assistant/usage").text + client.post("/api/assistant/chat", json=BODY).text
    assert '"provider_name":"WebNest AI"' in seen
    for secret in ("anthropic", "claude", "haiku", "groq", "gpt-oss"):
        assert secret not in seen.lower()


def test_the_general_space_needs_an_active_pass_too() -> None:
    provider = FakeProvider()
    assistant, payments = paid_service(FakePaymentStore(None), provider)
    body = {**BODY, "context": {"language": "general", "files": []}}
    response = client_for(payments, assistant).post("/api/assistant/chat", json=body)
    assert response.status_code == 402
    assert provider.requests == []


def test_a_402_frees_the_users_stream_slot() -> None:
    assistant, payments = paid_service(FakePaymentStore())
    client = client_for(payments, assistant)
    assert [client.post("/api/assistant/chat", json=BODY).status_code for _ in range(2)] == [402, 402]


def test_the_paywall_off_means_no_402_and_no_access_in_the_stream() -> None:
    store = FakePaymentStore()
    assistant, payments = paid_service(store, assistant_paywall_enabled=False)
    response = client_for(payments, assistant).post("/api/assistant/chat", json=BODY)
    assert response.status_code == 200
    usage = dict(parse_sse(response.text))["usage"]
    assert "access" not in usage
    assert store.charged == []


def test_paying_users_are_not_stopped_by_the_free_daily_limits() -> None:
    usage_store = FakeStore(calls=500, user_tokens=59_999)
    assistant, payments = paid_service(FakePaymentStore(AccessState(LATER, 2_000_000, 0)), usage_store=usage_store)
    assert client_for(payments, assistant).post("/api/assistant/chat", json=BODY).status_code == 200


def test_the_daily_cap_stops_a_paying_user_at_150000() -> None:
    usage_store = FakeStore(user_tokens=150_000)
    provider = FakeProvider()
    assistant, payments = paid_service(FakePaymentStore(AccessState(LATER, 2_000_000, 0)), provider, usage_store)
    response = client_for(payments, assistant).post("/api/assistant/chat", json=BODY)
    assert response.status_code == 429
    assert response.json() == {"detail": "You have used your AI assistant allowance for today."}
    assert int(response.headers["retry-after"]) > 0
    assert provider.requests == []


def test_the_weekly_cap_stops_a_paying_user_at_700000() -> None:
    usage_store = FakeStore()
    usage_store.week_tokens = 700_000
    provider = FakeProvider()
    assistant, payments = paid_service(FakePaymentStore(AccessState(LATER, 2_000_000, 0)), provider, usage_store)
    response = client_for(payments, assistant).post("/api/assistant/chat", json=BODY)
    assert response.status_code == 429
    assert response.json() == {"detail": "You have used your AI assistant allowance for this week. It frees up day by day."}
    assert int(response.headers["retry-after"]) > 0
    assert provider.requests == []


def test_the_caps_can_be_changed_by_settings() -> None:
    usage_store = FakeStore(user_tokens=150_000)
    assistant, payments = paid_service(FakePaymentStore(AccessState(LATER, 2_000_000, 0)), usage_store=usage_store, assistant_pass_daily_tokens=500_000)
    assert client_for(payments, assistant).post("/api/assistant/chat", json=BODY).status_code == 200


def test_the_default_caps_are_150000_a_day_and_700000_a_week() -> None:
    from core.config import Settings

    defaults = Settings(_env_file=None)
    assert (defaults.assistant_pass_tokens, defaults.assistant_pass_weekly_tokens, defaults.assistant_pass_daily_tokens) == (2_000_000, 700_000, 150_000)


def test_a_cap_blocks_chat_but_not_the_badge() -> None:
    usage_store = FakeStore(user_tokens=150_000)
    assistant, payments = paid_service(FakePaymentStore(AccessState(LATER, 2_000_000, 0)), usage_store=usage_store)
    assert client_for(payments, assistant).get("/api/assistant/membership").json()["is_prime"] is True


def test_the_global_daily_cap_still_stops_paying_users() -> None:
    usage_store = FakeStore(tokens=10**9)
    assistant, payments = paid_service(FakePaymentStore(AccessState(LATER, 2_000_000, 0)), usage_store=usage_store)
    assert client_for(payments, assistant).post("/api/assistant/chat", json=BODY).status_code == 503


# ---- counting tokens --------------------------------------------------------------------


def test_the_stream_usage_event_carries_the_new_total() -> None:
    store = FakePaymentStore(AccessState(LATER, 2_000_000, 1000))
    assistant, payments = paid_service(store)
    usage = dict(parse_sse(client_for(payments, assistant).post("/api/assistant/chat", json=BODY).text))["usage"]
    assert usage["access"]["tokens_used"] == 1150
    assert usage["access"]["active"] is True
    assert store.charged == [150], "charged once, though both the event and close() ask for it"


def test_tokens_are_counted_after_a_mid_reply_failure() -> None:
    script = [StreamOpened(), TextDelta("hi"), UsageEvent(100, 20), ProviderStreamError("provider_error")]
    store = FakePaymentStore(AccessState(LATER, 1000, 0))
    assistant, payments = paid_service(store, FakeProvider(script))
    assert parse_sse(client_for(payments, assistant).post("/api/assistant/chat", json=BODY).text)[-1][0] == "error"
    assert store.charged == [120]


def test_tokens_are_counted_after_a_disconnect() -> None:
    async def main() -> None:
        store = FakePaymentStore(AccessState(LATER, 1000, 0))
        assistant, _ = paid_service(store, FakeProvider([StreamOpened(), TextDelta("x" * 40)], hang=True))
        from schemas.assistant_schemas import ChatRequest

        stream = await assistant.open_chat(USER, ChatRequest.model_validate(BODY))
        events = stream.events()
        await anext(events)
        await events.aclose()
        await stream.close()
        assert len(store.charged) == 1
        assert store.charged[0] > 0

    asyncio.run(main())


def test_a_client_leaving_during_the_charge_does_not_lose_the_count() -> None:
    gate = asyncio.Event

    class SlowStore(FakePaymentStore):
        def __init__(self, access: AccessState) -> None:
            super().__init__(access)
            self.gate: asyncio.Event | None = None

        async def add_tokens(self, user_id: uuid.UUID, tokens: int) -> AccessState | None:
            assert self.gate is not None
            await self.gate.wait()
            return await super().add_tokens(user_id, tokens)

    async def main() -> None:
        from schemas.assistant_schemas import ChatRequest

        store = SlowStore(AccessState(LATER, 1000, 0))
        store.gate = gate()
        assistant, _ = paid_service(store, FakeProvider([StreamOpened(), UsageEvent(10, 5), FinishedEvent("stop")]))
        stream = await assistant.open_chat(USER, ChatRequest.model_validate(BODY))
        reading = asyncio.create_task(anext(stream.events()))
        await asyncio.sleep(0.01)
        reading.cancel()
        await asyncio.gather(reading, return_exceptions=True)
        closing = asyncio.create_task(stream.close())
        await asyncio.sleep(0.01)
        store.gate.set()
        await closing
        assert store.charged == [15]

    asyncio.run(main())


def test_a_refused_call_gives_its_reservation_back() -> None:
    from services.assistant_llm import ProviderRateLimitedError

    store = FakePaymentStore(AccessState(LATER, 1000, 100))
    assistant, payments = paid_service(store, FakeProvider([ProviderRateLimitedError(5)]))
    assert client_for(payments, assistant).post("/api/assistant/chat", json=BODY).status_code == 429
    assert store.access[USER].tokens_used == 100


def test_a_call_stopped_by_a_daily_cap_gives_its_reservation_back() -> None:
    store = FakePaymentStore(AccessState(LATER, 2_000_000, 100))
    assistant, payments = paid_service(store, usage_store=FakeStore(user_tokens=150_000))
    assert client_for(payments, assistant).post("/api/assistant/chat", json=BODY).status_code == 429
    assert store.access[USER].tokens_used == 100


def test_a_cancelled_call_gives_its_reservation_back() -> None:
    async def main() -> None:
        from schemas.assistant_schemas import ChatRequest

        store = FakePaymentStore(AccessState(LATER, 2_000_000, 100))
        assistant, _ = paid_service(store, FakeProvider([], hang=True))
        opening = asyncio.create_task(assistant.open_chat(USER, ChatRequest.model_validate(BODY)))
        await asyncio.sleep(0.01)
        assert store.access[USER].tokens_used > 100, "reserved while it waits"
        opening.cancel()
        await asyncio.gather(opening, return_exceptions=True)
        await asyncio.sleep(0.01)
        assert store.access[USER].tokens_used == 100

    asyncio.run(main())


def test_a_stuck_database_giving_back_does_not_hold_up_the_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from schemas.assistant_schemas import ChatRequest
    from services import assistant_service
    from services.assistant_llm import ProviderRateLimitedError
    from services.assistant_service import AssistantRateLimitedError

    monkeypatch.setattr(assistant_service, "_REFUSAL_WAIT_SECONDS", 0.02)

    class StuckStore(FakePaymentStore):
        def __init__(self, access: AccessState) -> None:
            super().__init__(access)
            self.gate: asyncio.Event | None = None

        async def add_tokens(self, user_id: uuid.UUID, tokens: int) -> AccessState | None:
            assert self.gate is not None
            await self.gate.wait()
            return await super().add_tokens(user_id, tokens)

    async def main() -> None:
        store = StuckStore(AccessState(LATER, 2_000_000, 100))
        store.gate = asyncio.Event()
        assistant, _ = paid_service(store, FakeProvider([ProviderRateLimitedError(5)]))
        with pytest.raises(AssistantRateLimitedError):
            await asyncio.wait_for(assistant.open_chat(USER, ChatRequest.model_validate(BODY)), 1)
        store.gate.set()
        await asyncio.sleep(0.01)
        assert store.access[USER].tokens_used == 100

    asyncio.run(main())


def test_a_reply_that_failed_with_nothing_delivered_costs_the_pass_nothing() -> None:
    store = FakePaymentStore(AccessState(LATER, 1000, 100))
    usage_store = FakeStore()
    assistant, payments = paid_service(store, FakeProvider([StreamOpened(), ProviderStreamError("provider_error")]), usage_store)
    assert parse_sse(client_for(payments, assistant).post("/api/assistant/chat", json=BODY).text)[-1][0] == "error"
    assert store.charged == [0]
    assert store.access[USER].tokens_used == 100
    assert usage_store.recorded[0][0] > 0, "the daily caps still count the estimate"


def test_a_reply_with_reported_usage_is_charged_even_if_it_failed() -> None:
    store = FakePaymentStore(AccessState(LATER, 1000, 0))
    assistant, payments = paid_service(store, FakeProvider([StreamOpened(), UsageEvent(30, 0), ProviderStreamError("provider_error")]))
    client_for(payments, assistant).post("/api/assistant/chat", json=BODY)
    assert store.charged == [30]


def test_paying_users_see_no_free_daily_limit() -> None:
    usage_store = FakeStore(calls=120)
    assistant, payments = paid_service(FakePaymentStore(AccessState(LATER, 2_000_000, 0)), usage_store=usage_store)
    client = client_for(payments, assistant)
    assert client.get("/api/assistant/usage").json()["calls_limit"] == 0
    usage = dict(parse_sse(client.post("/api/assistant/chat", json=BODY).text))["usage"]
    assert usage["calls_limit"] == 0


def test_free_users_still_see_their_daily_limit() -> None:
    assistant, payments = paid_service(FakePaymentStore(), assistant_paywall_enabled=False)
    client = client_for(payments, assistant)
    assert client.get("/api/assistant/usage").json()["calls_limit"] == 40
    assert dict(parse_sse(client.post("/api/assistant/chat", json=BODY).text))["usage"]["calls_limit"] == 40


def test_a_failed_charge_does_not_break_the_reply() -> None:
    store = FakePaymentStore(AccessState(LATER, 1000, 0))
    store.fail_charge = True
    assistant, payments = paid_service(store)
    events = parse_sse(client_for(payments, assistant).post("/api/assistant/chat", json=BODY).text)
    assert events[-1][0] == "done"
    assert "access" not in dict(events)["usage"]


# ---- reconciliation ---------------------------------------------------------------------


def reconcile_setup(payments_list: list[RazorpayPayment]) -> tuple[AssistantPaymentService, FakePaymentStore, FakeRazorpay]:
    store = FakePaymentStore()
    asyncio.run(store.save_order(NewOrder(USER, "order_1", 29900, "INR", 30, 2_000_000)))
    razorpay = FakeRazorpay()
    razorpay.payments["order_1"] = payments_list
    return payment_service(store, razorpay), store, razorpay


def test_reconciliation_recovers_a_captured_payment() -> None:
    service, store, _ = reconcile_setup([RazorpayPayment("pay_failed", "failed", 29900, "INR"), RazorpayPayment("pay_1", "captured", 29900, "INR")])
    assert asyncio.run(service.reconcile()) == 1
    assert store.grants == [("order_1", "pay_1")]
    assert asyncio.run(service.reconcile()) == 0, "a paid order is no longer open"


@pytest.mark.parametrize(
    "payment",
    [RazorpayPayment("pay_1", "authorized", 29900, "INR"), RazorpayPayment("pay_1", "failed", 29900, "INR"), RazorpayPayment("pay_1", "captured", 1, "INR"), RazorpayPayment("pay_1", "captured", 29900, "USD"), RazorpayPayment("", "captured", 29900, "INR")],
    ids=["authorized", "failed", "wrong_amount", "wrong_currency", "no_id"],
)
def test_reconciliation_grants_nothing_for_anything_else(payment: RazorpayPayment) -> None:
    service, store, _ = reconcile_setup([payment])
    assert asyncio.run(service.reconcile()) == 0
    assert store.grants == []


def test_reconciliation_survives_razorpay_being_down() -> None:
    service, store, razorpay = reconcile_setup([])
    razorpay.fail = True
    assert asyncio.run(service.reconcile()) == 0
    assert store.grants == []


def test_a_database_error_on_one_order_does_not_stop_the_rest() -> None:
    class FlakyStore(FakePaymentStore):
        async def grant(self, order_id: str, payment_id: str) -> bool:
            if order_id == "order_bad":
                raise DatabaseError("timed out")
            return await super().grant(order_id, payment_id)

    store = FlakyStore()
    razorpay = FakeRazorpay()
    for order_id, payment_id in (("order_bad", "pay_bad"), ("order_good", "pay_good")):
        asyncio.run(store.save_order(NewOrder(USER, order_id, 29900, "INR", 30, 2_000_000)))
        razorpay.payments[order_id] = [RazorpayPayment(payment_id, "captured", 29900, "INR")]
    assert asyncio.run(payment_service(store, razorpay).reconcile()) == 1
    assert store.grants == [("order_good", "pay_good")]


def test_reconciliation_checks_orders_a_few_at_a_time() -> None:
    running = {"now": 0, "peak": 0}

    class Counting(FakeRazorpay):
        async def order_payments(self, order_id: str) -> list[RazorpayPayment]:
            running["now"] += 1
            running["peak"] = max(running["peak"], running["now"])
            await asyncio.sleep(0.001)
            running["now"] -= 1
            return []

    store = FakePaymentStore()
    for index in range(30):
        asyncio.run(store.save_order(NewOrder(USER, f"order_{index}", 29900, "INR", 30, 2_000_000)))
    assert asyncio.run(payment_service(store, Counting()).reconcile()) == 0
    assert 1 < running["peak"] <= 5


def test_the_reconciliation_job_logs_a_failure_and_goes_no_further(caplog: pytest.LogCaptureFixture) -> None:
    service = payment_service()

    async def boom() -> int:
        raise RuntimeError("boom")

    service.reconcile = boom  # type: ignore[method-assign]
    asyncio.run(reconcile_payments(service))
    assert "Razorpay reconciliation failed: RuntimeError" in caplog.text


def test_the_reconciliation_job_runs_only_with_keys() -> None:
    scheduler = AsyncIOScheduler()
    register_payment_jobs(scheduler, payment_service(), False)
    assert scheduler.get_jobs() == []
    register_payment_jobs(scheduler, payment_service(), True)
    job = scheduler.get_job("assistant_payment_reconcile")
    assert job is not None and job.coalesce is True


# ---- the Razorpay client ----------------------------------------------------------------


def razorpay_client(handler: Any) -> HttpRazorpayClient:
    return HttpRazorpayClient(settings(), transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


def test_the_client_creates_an_order_with_basic_auth() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "order_abc"})

    async def main() -> None:
        client = razorpay_client(handler)
        assert await client.create_order(29900, "INR", "r1", {"user_id": "u"}) == "order_abc"
        await client.aclose()
        assert client._client is None

    asyncio.run(main())
    request = seen[0]
    assert str(request.url) == "https://api.razorpay.com/v1/orders"
    assert request.headers["authorization"].startswith("Basic ")
    assert json.loads(request.content) == {"amount": 29900, "currency": "INR", "receipt": "r1", "notes": {"user_id": "u"}}


def test_the_client_reads_an_orders_payments() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"entity": "collection", "count": 2, "items": [{"id": "pay_1", "status": "captured", "amount": 29900, "currency": "INR"}, "junk"]})

    assert asyncio.run(razorpay_client(handler).order_payments("order_1")) == [RazorpayPayment("pay_1", "captured", 29900, "INR")]


@pytest.mark.parametrize(
    "handler",
    [
        lambda _: httpx.Response(500, json={"error": {"description": "secret detail"}}),
        lambda _: httpx.Response(401, text="nope"),
        lambda _: httpx.Response(200, text="not json"),
        lambda _: httpx.Response(200, json=[1]),
        lambda _: httpx.Response(200, json={"id": 5}),
    ],
    ids=["500", "401", "not_json", "not_object", "no_id"],
)
def test_every_bad_answer_becomes_razorpay_error(handler: Any) -> None:
    async def main() -> None:
        with pytest.raises(RazorpayError) as raised:
            await razorpay_client(handler).create_order(1, "INR", "r", {})
        assert "secret detail" not in str(raised.value)

    asyncio.run(main())


def test_the_client_reads_one_payment_with_its_order() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "pay_1", "status": "captured", "amount": 29900, "currency": "INR", "order_id": "order_1", "email": "x@example.com"})

    assert asyncio.run(razorpay_client(handler).get_payment("pay_1")) == RazorpayPayment("pay_1", "captured", 29900, "INR", "order_1")
    assert str(seen[0].url) == "https://api.razorpay.com/v1/payments/pay_1"


def test_a_dropped_connection_becomes_razorpay_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    async def main() -> None:
        with pytest.raises(RazorpayError):
            await razorpay_client(handler).order_payments("o")

    asyncio.run(main())
