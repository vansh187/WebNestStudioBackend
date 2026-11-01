"""WebNest Prime against a real Postgres: what only real transactions can prove, such as verify
and the webhook racing for one payment. Uses EXAMS_TEST_DATABASE_URL - a throwaway database,
never the production one in .env - and is skipped when it is not set."""

import asyncio
import hashlib
import hmac
import json
import os
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

os.environ.setdefault("SUPABASE_URL", "postgresql://user:pass@localhost:5432/test")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from core.exceptions import DatabaseError
from database.assistant_payment_persistence import AccessState, NewOrder, SqlAssistantPaymentStore, WebhookEvent
from database.models import AssistantAccess, AssistantPayment, AssistantWebhookEvent, User
from database.session import Base
from schemas.assistant_schemas import VerifyRequest
from services.assistant_payment_service import AssistantPaymentService, OrderRateLimiter
from services.razorpay_client import RazorpayPayment

TEST_DB_URL = os.environ.get("EXAMS_TEST_DATABASE_URL", "")
TABLES = [User.__table__, AssistantPayment.__table__, AssistantAccess.__table__, AssistantWebhookEvent.__table__]
MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
PAYMENT_MIGRATIONS = [MIGRATIONS / "017_assistant_payments.sql", MIGRATIONS / "018_assistant_access_period.sql"]
KEY_SECRET = "test-key-secret"
WEBHOOK_SECRET = "test-webhook-secret"
TOKENS = 2_000_000

CAPTURED_PAYMENTS: dict[str, str] = {}


class CapturingRazorpay:
    """Says a payment was captured, on the order the test signed it for."""

    async def get_payment(self, payment_id: str) -> RazorpayPayment:
        return RazorpayPayment(payment_id, "captured", 29900, "INR", CAPTURED_PAYMENTS.get(payment_id))


pytestmark = pytest.mark.skipif(not TEST_DB_URL, reason="EXAMS_TEST_DATABASE_URL is not set")


class Db:
    def __init__(self) -> None:
        url = TEST_DB_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
        self.engine = create_async_engine(url, poolclass=NullPool)
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        self.store = SqlAssistantPaymentStore(self.sessions)
        settings = SimpleNamespace(
            assistant_enabled=True,
            assistant_provider="groq",
            assistant_groq_api_key="placeholder",
            assistant_anthropic_api_key="",
            assistant_paywall_enabled=True,
            assistant_price_paise=29900,
            assistant_pass_days=30,
            assistant_pass_tokens=TOKENS,
            assistant_razorpay_key_id="rzp_test",
            assistant_razorpay_key_secret=KEY_SECRET,
            assistant_razorpay_webhook_secret=WEBHOOK_SECRET,
            assistant_razorpay_webhook_secret_previous="",
        )
        self.service = AssistantPaymentService(settings, self.store, CapturingRazorpay(), OrderRateLimiter())  # type: ignore[arg-type]

    async def reset(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.drop_all(sync, tables=TABLES[1:]))
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=TABLES))
            await connection.execute(text("delete from users where email like 'prime-%@test.local'"))

    async def user(self) -> uuid.UUID:
        user_id = uuid.uuid4()
        async with self.sessions() as session:
            session.add(User(id=user_id, email=f"prime-{user_id}@test.local", password_hash="x"))
            await session.commit()
        return user_id

    async def order(self, user_id: uuid.UUID, days: int = 30, tokens: int = TOKENS) -> str:
        order_id = f"order_{uuid.uuid4().hex}"
        await self.store.save_order(NewOrder(user_id, order_id, 29900, "INR", days, tokens))
        return order_id

    async def payment(self, order_id: str) -> AssistantPayment:
        async with self.sessions() as session:
            return (await session.execute(select(AssistantPayment).where(AssistantPayment.razorpay_order_id == order_id))).scalar_one()

    async def expire(self, user_id: uuid.UUID) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(text("update assistant_access set expires_at = now() - interval '1 day' where user_id = :u"), {"u": user_id})

    async def other_connections(self) -> int:
        async with self.engine.connect() as connection:
            result = await connection.execute(
                text("select count(*) from pg_stat_activity where datname = current_database() and pid <> pg_backend_pid()")
            )
            return int(result.scalar_one())

    async def webhook(self, event_id: str, body: dict[str, Any]) -> None:
        raw = json.dumps(body).encode()
        await self.service.handle_webhook(raw, hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest(), event_id)


def scenario(body: Callable[[Db], Awaitable[None]]) -> None:
    async def main() -> None:
        db = Db()
        await db.reset()
        try:
            await body(db)
        finally:
            await db.engine.dispose()

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy() if os.name == "nt" else asyncio.DefaultEventLoopPolicy())
    asyncio.run(main())


def captured(order_id: str, payment_id: str = "pay_1", amount: int = 29900, currency: str = "INR") -> dict[str, Any]:
    return {"event": "payment.captured", "payload": {"payment": {"entity": {"id": payment_id, "order_id": order_id, "amount": amount, "currency": currency}}}}


def order_paid(order_id: str, payment_id: str = "pay_1", amount: int = 29900) -> dict[str, Any]:
    return {
        "event": "order.paid",
        "payload": {"payment": {"entity": {"id": payment_id}}, "order": {"entity": {"id": order_id, "amount": amount, "currency": "INR"}}},
    }


def failed(order_id: str) -> dict[str, Any]:
    return {"event": "payment.failed", "payload": {"payment": {"entity": {"id": "pay_x", "order_id": order_id, "amount": 29900, "currency": "INR"}}}}


def refund(order_id: str, status: str = "full", payment_id: str = "pay_1") -> dict[str, Any]:
    return {
        "event": "refund.processed",
        "payload": {
            "refund": {"entity": {"id": "rfnd_1", "payment_id": payment_id, "amount": 29900}},
            "payment": {"entity": {"id": payment_id, "order_id": order_id, "amount": 29900, "amount_refunded": 29900 if status == "full" else 100, "refund_status": status}},
        },
    }


def verify_request(order_id: str, payment_id: str = "pay_1") -> VerifyRequest:
    CAPTURED_PAYMENTS[payment_id] = order_id
    signature = hmac.new(KEY_SECRET.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()
    return VerifyRequest(razorpay_order_id=order_id, razorpay_payment_id=payment_id, razorpay_signature=signature)


def days_from_now(state: AccessState | None) -> float:
    assert state is not None
    return (state.expires_at - datetime.now(timezone.utc)).total_seconds() / 86400


def test_verify_and_webhook_at_the_same_moment_give_one_grant() -> None:
    async def body(db: Db) -> None:
        for _ in range(5):
            user_id = await db.user()
            order_id = await db.order(user_id)
            payment_id = f"pay_{uuid.uuid4().hex}"
            await asyncio.gather(
                db.service.verify(user_id, verify_request(order_id, payment_id)),
                db.webhook(f"evt_{uuid.uuid4()}", captured(order_id, payment_id)),
                db.webhook(f"evt_{uuid.uuid4()}", order_paid(order_id, payment_id)),
                db.store.grant(order_id, payment_id),
            )
            state = await db.store.get_access(user_id)
            assert state is not None
            assert state.tokens_limit == TOKENS, "one pass, not several"
            assert 29.9 < days_from_now(state) < 30.1
        assert await db.other_connections() == 0

    scenario(body)


def test_one_payment_can_not_pay_for_two_orders() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        first, second = await db.order(user_id), await db.order(user_id)
        assert await db.store.grant(first, "pay_same") is True
        before = await db.store.get_access(user_id)
        with pytest.raises(DatabaseError):  # razorpay_payment_id is unique
            await db.store.grant(second, "pay_same")
        assert await db.store.get_access(user_id) == before
        assert (await db.payment(second)).status == "created"
        assert await db.other_connections() == 0

    scenario(body)


def test_a_second_payment_id_on_a_paid_order_grants_nothing() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        order_id = await db.order(user_id)
        assert await db.store.grant(order_id, "pay_1") is True
        before = await db.store.get_access(user_id)
        assert await db.store.grant(order_id, "pay_2") is False
        assert await db.store.get_access(user_id) == before
        assert (await db.payment(order_id)).razorpay_payment_id == "pay_1"

    scenario(body)


def test_many_webhook_retries_and_verifies_at_once_give_one_grant() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        order_id = await db.order(user_id)
        retries = [db.webhook("evt_same", captured(order_id)) for _ in range(5)]
        verifies = [db.service.verify(user_id, verify_request(order_id)) for _ in range(5)]
        results = await asyncio.gather(*retries, *verifies, return_exceptions=True)
        assert [result for result in results if isinstance(result, Exception)] == []
        state = await db.store.get_access(user_id)
        assert state is not None and state.tokens_limit == TOKENS
        assert days_from_now(state) < 30.1
        async with db.engine.connect() as connection:
            events = await connection.execute(text("select count(*) from assistant_webhook_events where event_id = 'evt_same'"))
            assert events.scalar_one() == 1
        assert await db.other_connections() == 0

    scenario(body)


def test_a_grant_records_the_payment() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        order_id = await db.order(user_id)
        assert await db.store.grant(order_id, "pay_1") is True
        assert await db.store.grant(order_id, "pay_1") is False
        row = await db.payment(order_id)
        assert (row.status, row.razorpay_payment_id) == ("paid", "pay_1")
        assert row.paid_at is not None

    scenario(body)


def test_a_repeated_event_id_changes_nothing() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        first, second = await db.order(user_id), await db.order(user_id)
        await db.webhook("evt_1", captured(first, "pay_1"))
        before = await db.store.get_access(user_id)
        # Same event id with another order: if it weren't deduped, this would grant again.
        await db.webhook("evt_1", captured(second, "pay_2"))
        assert await db.store.get_access(user_id) == before
        assert (await db.payment(second)).status == "created"

    scenario(body)


@pytest.mark.parametrize("paid_first", [True, False], ids=["order_paid_first", "captured_first"])
def test_order_paid_and_payment_captured_give_one_grant_in_either_order(paid_first: bool) -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        order_id = await db.order(user_id)
        events = [order_paid(order_id), captured(order_id)]
        for index, event in enumerate(events if paid_first else reversed(events)):
            await db.webhook(f"evt_{index}", event)
        state = await db.store.get_access(user_id)
        assert state is not None and state.tokens_limit == TOKENS
        assert days_from_now(state) < 30.1

    scenario(body)


def test_an_unknown_order_gets_a_quiet_success_and_grants_nothing() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        await db.webhook("evt_1", captured("order_someone_elses"))
        assert await db.store.get_access(user_id) is None

    scenario(body)


@pytest.mark.parametrize(("amount", "currency"), [(1, "INR"), (29900, "USD")], ids=["amount", "currency"])
def test_a_mismatched_amount_or_currency_grants_nothing(amount: int, currency: str) -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        order_id = await db.order(user_id)
        await db.webhook("evt_1", captured(order_id, amount=amount, currency=currency))
        assert await db.store.get_access(user_id) is None
        assert (await db.payment(order_id)).status == "created"

    scenario(body)


def test_a_failed_payment_is_recorded_and_a_retry_can_still_pay() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        order_id = await db.order(user_id)
        await db.webhook("evt_1", failed(order_id))
        assert (await db.payment(order_id)).status == "failed"
        await db.webhook("evt_2", captured(order_id))
        assert (await db.payment(order_id)).status == "paid"
        assert await db.store.get_access(user_id) is not None

    scenario(body)


def test_a_late_failure_event_never_reduces_paid_access() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        order_id = await db.order(user_id)
        await db.webhook("evt_1", captured(order_id))
        before = await db.store.get_access(user_id)
        await db.webhook("evt_2", failed(order_id))
        assert (await db.payment(order_id)).status == "paid"
        assert await db.store.get_access(user_id) == before

    scenario(body)


def test_a_full_refund_ends_the_access_it_gave() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        order_id = await db.order(user_id)
        await db.webhook("evt_1", captured(order_id))
        await db.webhook("evt_2", refund(order_id))
        assert (await db.payment(order_id)).status == "refunded"
        state = await db.store.get_access(user_id)
        assert days_from_now(state) < 0.1
        access = await db.service.get_access(user_id)
        assert (access.active, access.is_prime) == (False, False)

    scenario(body)


def test_a_refund_of_one_pass_leaves_the_other_pass() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        first, second = await db.order(user_id), await db.order(user_id)
        await db.store.grant(first, "pay_1")
        await db.store.grant(second, "pay_2")
        await db.webhook("evt_1", refund(second, payment_id="pay_2"))
        state = await db.store.get_access(user_id)
        assert state is not None and state.tokens_limit == TOKENS
        assert 29.9 < days_from_now(state) < 30.1
        assert (await db.service.get_access(user_id)).active is True

    scenario(body)


def test_a_repeated_refund_takes_back_once() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        first, second = await db.order(user_id), await db.order(user_id)
        await db.store.grant(first, "pay_1")
        await db.store.grant(second, "pay_2")
        await db.webhook("evt_1", refund(second, payment_id="pay_2"))
        before = await db.store.get_access(user_id)
        await db.webhook("evt_2", refund(second, payment_id="pay_2"))
        assert await db.store.get_access(user_id) == before

    scenario(body)


def test_refunding_a_second_payment_on_an_order_leaves_the_pass_from_the_first() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        order_id = await db.order(user_id)
        await db.webhook("evt_1", captured(order_id, "pay_first"))
        before = await db.store.get_access(user_id)
        await db.webhook("evt_2", refund(order_id, payment_id="pay_late"))
        assert await db.store.get_access(user_id) == before
        assert (await db.payment(order_id)).status == "paid"
        assert await db.store.apply_webhook(WebhookEvent("evt_3", "refund.processed", order_id, "pay_late", full_refund=True)) == "refund_other_payment"

    scenario(body)


def test_refunding_a_lapsed_pass_leaves_the_newer_one_alone() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        old, new = await db.order(user_id), await db.order(user_id)
        await db.store.grant(old, "pay_old")
        await db.expire(user_id)
        await db.store.grant(new, "pay_new")
        before = await db.store.get_access(user_id)
        await db.webhook("evt_1", refund(old, payment_id="pay_old"))
        assert await db.store.get_access(user_id) == before
        assert (await db.payment(old)).status == "refunded"
        assert (await db.service.get_access(user_id)).active is True

    scenario(body)


def test_refunding_the_newer_pass_after_a_lapse_still_ends_it() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        old, new = await db.order(user_id), await db.order(user_id)
        await db.store.grant(old, "pay_old")
        await db.expire(user_id)
        await db.store.grant(new, "pay_new")
        await db.webhook("evt_1", refund(new, payment_id="pay_new"))
        assert (await db.service.get_access(user_id)).active is False

    scenario(body)


def test_refunding_the_older_of_two_stacked_passes_takes_back_its_share() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        first, second = await db.order(user_id), await db.order(user_id)
        await db.store.grant(first, "pay_1")
        await db.store.grant(second, "pay_2")
        await db.webhook("evt_1", refund(first, payment_id="pay_1"))
        state = await db.store.get_access(user_id)
        assert state is not None and state.tokens_limit == TOKENS
        assert 29.9 < days_from_now(state) < 30.1

    scenario(body)


def test_a_partial_refund_keeps_the_access() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        order_id = await db.order(user_id)
        await db.webhook("evt_1", captured(order_id))
        before = await db.store.get_access(user_id)
        await db.webhook("evt_2", refund(order_id, "partial"))
        assert (await db.payment(order_id)).status == "paid"
        assert await db.store.get_access(user_id) == before

    scenario(body)


def test_a_refund_arriving_before_the_capture_stops_the_grant() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        order_id = await db.order(user_id)
        await db.webhook("evt_1", refund(order_id))
        await db.webhook("evt_2", captured(order_id))
        assert (await db.payment(order_id)).status == "refunded"
        assert await db.store.get_access(user_id) is None

    scenario(body)


def test_paying_during_a_pass_carries_the_days_and_unused_tokens_over() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        first, second = await db.order(user_id), await db.order(user_id)
        await db.store.grant(first, "pay_1")
        await db.store.add_tokens(user_id, 500_000)
        await db.store.grant(second, "pay_2")
        state = await db.store.get_access(user_id)
        assert state is not None
        assert (state.tokens_limit, state.tokens_used) == (TOKENS + 1_500_000, 0)
        assert 59.9 < days_from_now(state) < 60.1

    scenario(body)


def test_paying_after_expiry_resets_the_allowance_and_loses_old_tokens() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        first, second = await db.order(user_id), await db.order(user_id)
        await db.store.grant(first, "pay_1")
        await db.expire(user_id)
        await db.store.grant(second, "pay_2")
        state = await db.store.get_access(user_id)
        assert state is not None
        assert (state.tokens_limit, state.tokens_used) == (TOKENS, 0)
        assert 29.9 < days_from_now(state) < 30.1

    scenario(body)


def test_the_days_and_tokens_are_those_saved_on_the_order() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        order_id = await db.order(user_id, days=7, tokens=123)
        await db.store.grant(order_id, "pay_1")
        state = await db.store.get_access(user_id)
        assert state is not None and state.tokens_limit == 123
        assert 6.9 < days_from_now(state) < 7.1

    scenario(body)


def test_parallel_calls_do_not_lose_token_counts() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        await db.store.grant(await db.order(user_id), "pay_1")
        await asyncio.gather(*(db.store.add_tokens(user_id, 100) for _ in range(20)))
        state = await db.store.get_access(user_id)
        assert state is not None and state.tokens_used == 2000
        assert await db.other_connections() == 0

    scenario(body)


def test_parallel_reservations_can_only_overrun_by_the_last_call() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        await db.store.grant(await db.order(user_id), "pay_1")
        await db.store.add_tokens(user_id, TOKENS - 1)
        results = await asyncio.gather(*(db.store.reserve_tokens(user_id, 7000) for _ in range(30)))
        assert results.count(True) == 1
        state = await db.store.get_access(user_id)
        assert state is not None and state.tokens_used == TOKENS - 1 + 7000
        assert await db.other_connections() == 0

    scenario(body)


def test_an_expired_or_missing_pass_can_not_be_reserved() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        assert await db.store.reserve_tokens(user_id, 10) is False
        await db.store.grant(await db.order(user_id), "pay_1")
        assert await db.store.reserve_tokens(user_id, 10) is True
        await db.expire(user_id)
        assert await db.store.reserve_tokens(user_id, 10) is False

    scenario(body)


def test_giving_back_tokens_never_goes_below_zero() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        await db.store.grant(await db.order(user_id), "pay_1")
        state = await db.store.add_tokens(user_id, -500)
        assert state is not None and state.tokens_used == 0

    scenario(body)


def test_adding_tokens_with_no_pass_is_a_quiet_nothing() -> None:
    async def body(db: Db) -> None:
        assert await db.store.add_tokens(await db.user(), 100) is None

    scenario(body)


def test_a_user_can_only_find_their_own_order() -> None:
    async def body(db: Db) -> None:
        owner, other = await db.user(), await db.user()
        order_id = await db.order(owner)
        assert await db.store.find_user_order(order_id, other) is None
        found = await db.store.find_user_order(order_id, owner)
        assert found is not None and found.order_id == order_id

    scenario(body)


def test_open_orders_are_unpaid_and_recent_only() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        fresh, old, paid, failing = [await db.order(user_id) for _ in range(4)]
        await db.store.grant(paid, "pay_1")
        await db.webhook("evt_1", failed(failing))
        async with db.engine.begin() as connection:
            await connection.execute(text("update assistant_payments set created_at = now() - interval '3 days' where razorpay_order_id = :o"), {"o": old})
        found = await db.store.open_orders(datetime.now(timezone.utc) - timedelta(hours=24), 10, 5)
        assert {order.order_id for order in found} == {fresh, failing}

    scenario(body)


def test_one_users_abandoned_orders_cant_crowd_out_another_users_order() -> None:
    async def body(db: Db) -> None:
        patient, spammer = await db.user(), await db.user()
        paid_for = await db.order(patient)
        spam = [await db.order(spammer) for _ in range(30)]
        found = {order.order_id for order in await db.store.open_orders(datetime.now(timezone.utc) - timedelta(hours=24), 10, 5)}
        assert paid_for in found
        assert len(found & set(spam)) == 5, "the newest few of that user's"

    scenario(body)


def test_deleting_an_account_keeps_the_payment_record_with_no_link() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        order_id = await db.order(user_id)
        await db.store.grant(order_id, "pay_1")
        async with db.engine.begin() as connection:
            await connection.execute(text("delete from users where id = :u"), {"u": user_id})
        row = await db.payment(order_id)
        assert (row.user_id, row.status) == (None, "paid")
        assert await db.store.get_access(user_id) is None

    scenario(body)


def test_a_payment_arriving_after_the_account_is_deleted_does_not_fail() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        order_id = await db.order(user_id)
        async with db.engine.begin() as connection:
            await connection.execute(text("delete from users where id = :u"), {"u": user_id})
        await db.webhook("evt_1", captured(order_id))
        assert (await db.payment(order_id)).status == "paid"
        assert await db.store.apply_webhook(WebhookEvent("evt_2", "payment.captured", await db.order(await db.user()), "pay_2", 29900, "INR")) == "granted"

    scenario(body)


def test_money_taken_for_a_deleted_account_is_flagged_for_a_refund(caplog: pytest.LogCaptureFixture) -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        order_id = await db.order(user_id)
        async with db.engine.begin() as connection:
            await connection.execute(text("delete from users where id = :u"), {"u": user_id})
        event = WebhookEvent("evt_1", "payment.captured", order_id, "pay_1", 29900, "INR")
        assert await db.store.apply_webhook(event) == "no_account"
        assert "refund it by hand" in caplog.text
        assert await db.store.grant(order_id, "pay_1") is False

    scenario(body)


def test_a_failed_webhook_leaves_no_dedupe_row_so_the_retry_works() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        order_id = await db.order(user_id)
        other = await db.order(user_id)
        await db.store.grant(other, "pay_taken")
        with pytest.raises(DatabaseError):  # the payment id is already on another order
            await db.webhook("evt_1", captured(order_id, "pay_taken"))
        await db.webhook("evt_1", captured(order_id, "pay_new"))
        assert (await db.payment(order_id)).status == "paid"
        assert await db.other_connections() == 0

    scenario(body)


def test_a_stuck_database_times_out_and_frees_its_connection() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        order_id = await db.order(user_id)
        impatient = SqlAssistantPaymentStore(db.sessions, timeout_seconds=0.3)
        async with db.sessions() as blocker:
            await blocker.execute(text("lock table assistant_payments in access exclusive mode"))
            with pytest.raises(DatabaseError):
                await impatient.grant(order_id, "pay_1")
            await blocker.rollback()
        assert await db.other_connections() == 0
        assert (await db.payment(order_id)).status == "created"

    scenario(body)


def test_webhook_event_with_no_order_id_is_recorded_and_harmless() -> None:
    async def body(db: Db) -> None:
        assert await db.store.apply_webhook(WebhookEvent("evt_1", "payment.captured")) == "malformed"
        assert await db.store.apply_webhook(WebhookEvent("evt_1", "payment.captured")) == "duplicate"
        assert await db.store.apply_webhook(WebhookEvent("evt_2", "subscription.charged")) == "ignored"

    scenario(body)


def test_the_period_migration_backfills_so_an_earlier_pass_can_still_be_refunded() -> None:
    async def body(db: Db) -> None:
        user_id = await db.user()
        order_id = await db.order(user_id)
        await db.store.grant(order_id, "pay_1")
        async with db.engine.begin() as connection:
            # As a pass bought before the column existed looks once it's added.
            await connection.execute(text("update assistant_access set period_started_at = now() + interval '1 hour'"))
        async with db.engine.connect() as connection:
            autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
            raw = await autocommit.get_raw_connection()
            await raw.driver_connection.execute(PAYMENT_MIGRATIONS[1].read_text(encoding="utf-8"))
        await db.webhook("evt_1", refund(order_id))
        assert (await db.service.get_access(user_id)).active is False

    scenario(body)


def test_the_migration_builds_the_same_tables_as_the_models_and_can_rerun() -> None:
    names = ("assistant_payments", "assistant_access", "assistant_webhook_events")

    async def shape(db: Db) -> list[Any]:
        async with db.engine.connect() as connection:
            columns = await connection.execute(
                text(
                    "select table_name, column_name, data_type, is_nullable, column_default from information_schema.columns "
                    "where table_name = any(:t) order by 1, 2"
                ),
                {"t": list(names)},
            )
            indexes = await connection.execute(
                text("select tablename, indexname, split_part(indexdef, ' USING ', 2) from pg_indexes where tablename = any(:t) order by 1, 2"), {"t": list(names)}
            )
            constraints = await connection.execute(
                text(
                    "select conrelid::regclass::text, conname, pg_get_constraintdef(oid) from pg_constraint "
                    "where conrelid::regclass::text = any(:t) order by 1, 2"
                ),
                {"t": list(names)},
            )
            return [[tuple(row) for row in columns], [tuple(row) for row in indexes], [tuple(row) for row in constraints]]

    async def run_migration(db: Db) -> None:
        async with db.engine.connect() as connection:
            autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
            raw = await autocommit.get_raw_connection()
            for migration in PAYMENT_MIGRATIONS:
                await raw.driver_connection.execute(migration.read_text(encoding="utf-8"))

    async def body(db: Db) -> None:
        from_model = await shape(db)
        async with db.engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.drop_all(sync, tables=TABLES[1:]))
        await run_migration(db)
        await run_migration(db)
        assert await shape(db) == from_model
        # Startup's create_all must leave the migrated tables exactly as they are.
        async with db.engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=TABLES))
        assert await shape(db) == from_model
        async with db.engine.connect() as connection:
            secured = await connection.execute(text("select relname, relrowsecurity from pg_class where relname = any(:t)"), {"t": list(names)})
            assert all(row[1] is True for row in secured)
        user_id = await db.user()
        assert await db.store.grant(await db.order(user_id), "pay_1") is True, "the owner bypasses row level security"

    scenario(body)
