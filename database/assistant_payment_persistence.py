"""WebNest Prime payments and access. Each method opens its own short session with a time
limit, so no connection is held during a call to Razorpay. Verify, the webhook and the
reconciliation job all grant through _grant, so a payment only ever counts once."""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol

from sqlalchemy import DateTime, case, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import DatabaseError
from database.models import AssistantAccess, AssistantPayment, AssistantWebhookEvent

logger = logging.getLogger("webnest.assistant.payments")

DB_TIMEOUT_SECONDS = 10.0
# Razorpay treats a webhook as failed after 5 seconds, and sends it again.
WEBHOOK_TIMEOUT_SECONDS = 4.0
_ERROR = "Could not reach the payment records right now. Please try again shortly."

GRANT_EVENTS = frozenset({"payment.captured", "order.paid"})
WebhookOutcome = Literal[
    "duplicate",
    "granted",
    "already_granted",
    "no_account",
    "unknown_order",
    "mismatch",
    "malformed",
    "failed_recorded",
    "refunded",
    "partial_refund",
    "refund_other_payment",
    "ignored",
]
_GrantResult = Literal["granted", "already_granted", "no_account"]


@dataclass(frozen=True)
class AccessState:
    expires_at: datetime
    tokens_limit: int
    tokens_used: int


@dataclass(frozen=True)
class SavedOrder:
    order_id: str
    user_id: uuid.UUID | None
    amount_paise: int
    currency: str
    status: str


@dataclass(frozen=True)
class NewOrder:
    user_id: uuid.UUID
    order_id: str
    amount_paise: int
    currency: str
    pass_days: int
    pass_tokens: int


@dataclass(frozen=True)
class WebhookEvent:
    """What a signed webhook said, reduced to the fields we act on."""

    event_id: str
    event_type: str
    order_id: str | None = None
    payment_id: str | None = None
    amount: int | None = None
    currency: str | None = None
    full_refund: bool = False


class AssistantPaymentStore(Protocol):
    async def get_access(self, user_id: uuid.UUID) -> AccessState | None: ...

    async def save_order(self, order: NewOrder) -> None: ...

    async def find_user_order(self, order_id: str, user_id: uuid.UUID) -> SavedOrder | None: ...

    async def grant(self, order_id: str, payment_id: str) -> bool: ...

    async def reserve_tokens(self, user_id: uuid.UUID, tokens: int) -> bool: ...

    async def add_tokens(self, user_id: uuid.UUID, tokens: int) -> AccessState | None: ...

    async def apply_webhook(self, event: WebhookEvent) -> WebhookOutcome: ...

    async def open_orders(self, since: datetime, limit: int, per_user: int) -> list[SavedOrder]: ...


def _access_state(row: AssistantAccess | None) -> AccessState | None:
    return None if row is None else AccessState(row.expires_at, row.tokens_limit, row.tokens_used)


def _saved_order(row: AssistantPayment) -> SavedOrder:
    return SavedOrder(row.razorpay_order_id, row.user_id, row.amount_paise, row.currency, row.status)


class SqlAssistantPaymentStore:
    def __init__(self, session_factory: Callable[[], AsyncSession], timeout_seconds: float = DB_TIMEOUT_SECONDS) -> None:
        self._session_factory = session_factory
        self._timeout_seconds = timeout_seconds

    @asynccontextmanager
    async def _session(self, timeout_seconds: float | None = None) -> AsyncIterator[AsyncSession]:
        """A session that is always closed, which rolls back anything not committed and
        returns its connection, however the block ends."""
        try:
            async with asyncio.timeout(self._timeout_seconds if timeout_seconds is None else timeout_seconds):
                async with self._session_factory() as session:
                    yield session
        except (SQLAlchemyError, OSError, TimeoutError) as exc:
            raise DatabaseError(_ERROR) from exc

    async def get_access(self, user_id: uuid.UUID) -> AccessState | None:
        async with self._session() as session:
            return _access_state(await session.get(AssistantAccess, user_id))

    async def save_order(self, order: NewOrder) -> None:
        async with self._session() as session:
            session.add(
                AssistantPayment(
                    user_id=order.user_id,
                    razorpay_order_id=order.order_id,
                    amount_paise=order.amount_paise,
                    currency=order.currency,
                    pass_days=order.pass_days,
                    pass_tokens=order.pass_tokens,
                )
            )
            await session.commit()

    async def find_user_order(self, order_id: str, user_id: uuid.UUID) -> SavedOrder | None:
        statement = select(AssistantPayment).where(AssistantPayment.razorpay_order_id == order_id, AssistantPayment.user_id == user_id)
        async with self._session() as session:
            row = (await session.execute(statement)).scalar_one_or_none()
            return None if row is None else _saved_order(row)

    async def grant(self, order_id: str, payment_id: str) -> bool:
        """True when this call gave the access, False when it was already handled."""
        async with self._session() as session:
            result = await self._grant(session, order_id, payment_id)
            await session.commit()
            return result == "granted"

    async def reserve_tokens(self, user_id: uuid.UUID, tokens: int) -> bool:
        """Counts a call's most before it starts, if the pass is usable. Checking and counting
        in one statement means parallel calls, even on different servers, each see the ones
        before them, so a pass can only ever run over by its last call."""
        statement = (
            update(AssistantAccess)
            .where(
                AssistantAccess.user_id == user_id,
                AssistantAccess.expires_at > func.now(),
                AssistantAccess.tokens_used < AssistantAccess.tokens_limit,
            )
            .values(tokens_used=AssistantAccess.tokens_used + tokens, updated_at=func.now())
            .returning(AssistantAccess.user_id)
        )
        async with self._session() as session:
            row = (await session.execute(statement)).one_or_none()
            await session.commit()
            return row is not None

    async def add_tokens(self, user_id: uuid.UUID, tokens: int) -> AccessState | None:
        """One atomic update, so parallel calls can't lose each other's counts. `tokens` is
        negative when a call gives back part of what it reserved."""
        statement = (
            update(AssistantAccess)
            .where(AssistantAccess.user_id == user_id)
            .values(tokens_used=func.greatest(AssistantAccess.tokens_used + tokens, 0), updated_at=func.now())
            .returning(AssistantAccess.expires_at, AssistantAccess.tokens_limit, AssistantAccess.tokens_used)
        )
        async with self._session() as session:
            row = (await session.execute(statement)).one_or_none()
            await session.commit()
            return None if row is None else AccessState(row.expires_at, row.tokens_limit, row.tokens_used)

    async def apply_webhook(self, event: WebhookEvent) -> WebhookOutcome:
        """The dedupe row and the event's effect commit together, so a failure here means
        Razorpay's retry starts clean, and a repeated event changes nothing."""
        claim = (
            insert(AssistantWebhookEvent)
            .values(event_id=event.event_id, event_type=event.event_type)
            .on_conflict_do_nothing(index_elements=[AssistantWebhookEvent.event_id])
            .returning(AssistantWebhookEvent.event_id)
        )
        async with self._session(WEBHOOK_TIMEOUT_SECONDS) as session:
            if (await session.execute(claim)).one_or_none() is None:
                return "duplicate"
            outcome = await self._apply(session, event)
            await session.commit()
            return outcome

    async def open_orders(self, since: datetime, limit: int, per_user: int) -> list[SavedOrder]:
        """Recent unpaid orders, newest first, with a cap per user so one user's abandoned
        orders can't push another user's paid order out of the check."""
        newest_first = func.row_number().over(partition_by=AssistantPayment.user_id, order_by=AssistantPayment.created_at.desc())
        ranked = (
            select(AssistantPayment.id, newest_first.label("position"))
            .where(AssistantPayment.status.in_(("created", "failed")), AssistantPayment.created_at >= since)
            .subquery()
        )
        statement = (
            select(AssistantPayment)
            .join(ranked, AssistantPayment.id == ranked.c.id)
            .where(ranked.c.position <= per_user)
            .order_by(AssistantPayment.created_at.desc())
            .limit(limit)
        )
        async with self._session() as session:
            return [_saved_order(row) for row in (await session.execute(statement)).scalars()]

    async def _apply(self, session: AsyncSession, event: WebhookEvent) -> WebhookOutcome:
        if event.event_type not in GRANT_EVENTS and event.event_type not in ("payment.failed", "refund.processed"):
            return "ignored"
        if event.order_id is None:
            return "malformed"
        # Locked, so a refund and a grant for one order can't interleave.
        order = (
            await session.execute(select(AssistantPayment).where(AssistantPayment.razorpay_order_id == event.order_id).with_for_update())
        ).scalar_one_or_none()
        if order is None:
            return "unknown_order"
        if event.event_type in GRANT_EVENTS:
            if event.payment_id is None:
                return "malformed"
            if event.amount != order.amount_paise or event.currency != order.currency:
                return "mismatch"
            return await self._grant(session, event.order_id, event.payment_id)
        if event.event_type == "payment.failed":
            if order.status == "created":
                order.status = "failed"
            return "failed_recorded"
        if not event.full_refund:
            return "partial_refund"
        # An order can see a second, late payment that Razorpay refunds by itself. Refunding
        # that one must not touch the pass the first payment bought.
        if order.status == "paid" and event.payment_id != order.razorpay_payment_id:
            return "refund_other_payment"
        await self._refund(session, order)
        return "refunded"

    async def _refund(self, session: AsyncSession, order: AssistantPayment) -> None:
        """Takes back what this payment gave, if that is still in effect. Paid before the
        current run of passes began means it had lapsed, so the newer pass isn't touched. An
        order not yet paid is only marked, which stops a late capture event from granting it."""
        if order.status == "paid" and order.user_id is not None and order.paid_at is not None:
            await session.execute(
                update(AssistantAccess)
                .where(AssistantAccess.user_id == order.user_id, AssistantAccess.period_started_at <= order.paid_at)
                .values(
                    expires_at=AssistantAccess.expires_at - timedelta(days=order.pass_days),
                    tokens_limit=func.greatest(AssistantAccess.tokens_limit - order.pass_tokens, 0),
                    updated_at=func.now(),
                )
            )
        order.status = "refunded"

    async def _grant(self, session: AsyncSession, order_id: str, payment_id: str) -> _GrantResult:
        """The update's row lock makes verify and the webhook take turns. Whichever comes
        second matches no row and grants nothing. Days and tokens come from the order, so
        a later settings change doesn't alter what was already bought."""
        paid = (
            await session.execute(
                update(AssistantPayment)
                .where(AssistantPayment.razorpay_order_id == order_id, AssistantPayment.status.not_in(("paid", "refunded")))
                .values(status="paid", razorpay_payment_id=payment_id, paid_at=func.now())
                .returning(AssistantPayment.user_id, AssistantPayment.pass_days, AssistantPayment.pass_tokens)
            )
        ).one_or_none()
        if paid is None:
            return "already_granted"
        if paid.user_id is None:
            logger.error("Money taken for a deleted account, refund it by hand: order=%s payment=%s", order_id, payment_id)
            return "no_account"
        days = timedelta(days=paid.pass_days)
        still_running = AssistantAccess.expires_at > func.now()
        upsert = insert(AssistantAccess).values(
            user_id=paid.user_id, expires_at=func.now() + days, tokens_limit=paid.pass_tokens, tokens_used=0, period_started_at=func.now()
        )
        await session.execute(
            upsert.on_conflict_do_update(
                index_elements=[AssistantAccess.user_id],
                set_={
                    "tokens_limit": case(
                        (still_running, func.greatest(AssistantAccess.tokens_limit - AssistantAccess.tokens_used, 0) + paid.pass_tokens),
                        else_=paid.pass_tokens,
                    ),
                    "tokens_used": 0,
                    "period_started_at": case((still_running, AssistantAccess.period_started_at), else_=func.now()),
                    "expires_at": func.greatest(AssistantAccess.expires_at, func.now(), type_=DateTime(timezone=True)) + days,
                    "updated_at": func.now(),
                },
            )
        )
        return "granted"
