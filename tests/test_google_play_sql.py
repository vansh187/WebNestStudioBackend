"""Google Play purchases and AI reply reports against a real Postgres: what only real
transactions can prove, such as one grant when many requests race for a token, and the whole
verify path working end to end through the real router, store and HTTP client, with Google
mocked at the network. Uses EXAMS_TEST_DATABASE_URL - a throwaway database, never the
production one in .env - and is skipped when it is not set."""

import asyncio
import os
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

os.environ.setdefault("SUPABASE_URL", "postgresql://user:pass@localhost:5432/test")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from api.assistant_payment_router import router as payment_router
from api.assistant_router import get_assistant_user_id
from core.dependencies import get_google_play_payment_service
from core.error_handlers import register_error_handlers
from database.assistant_payment_persistence import NewOrder, NewPlayPurchase, PlayPurchaseRecord, SqlAssistantPaymentStore
from database.assistant_report_persistence import NewAssistantReport, SqlAssistantReportStore
from database.models import (
    AssistantAccess,
    AssistantCallLog,
    AssistantPayment,
    AssistantPlayPurchase,
    AssistantReport,
    AssistantWebhookEvent,
    User,
)
from database.session import Base
from services.assistant_payment_service import AssistantPaymentService, OrderRateLimiter
from services.google_play_client import HttpGooglePlayClient
from services.google_play_payment_service import GooglePlayPaymentService
from services.user_rate_limiter import UserRateLimiter
from tests.google_play_fakes import PACKAGE, PRODUCT, TOKEN_URI, service_account_json

TEST_DB_URL = os.environ.get("EXAMS_TEST_DATABASE_URL", "")
TABLES = [
    User.__table__,
    AssistantPayment.__table__,
    AssistantAccess.__table__,
    AssistantWebhookEvent.__table__,
    AssistantPlayPurchase.__table__,
    AssistantReport.__table__,
    AssistantCallLog.__table__,
]
MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
NEW_MIGRATIONS = [MIGRATIONS / "021_assistant_play_purchases.sql", MIGRATIONS / "022_assistant_reports.sql"]
TOKENS = 2_000_000

pytestmark = pytest.mark.skipif(not TEST_DB_URL, reason="EXAMS_TEST_DATABASE_URL is not set")

SETTINGS = SimpleNamespace(
    assistant_enabled=True,
    assistant_provider="groq",
    assistant_groq_api_key="placeholder",
    assistant_anthropic_api_key="",
    assistant_paywall_enabled=True,
    assistant_price_paise=29900,
    assistant_pass_days=30,
    assistant_pass_tokens=TOKENS,
    assistant_razorpay_key_id="",
    assistant_razorpay_key_secret="",
    assistant_razorpay_webhook_secret="",
    assistant_razorpay_webhook_secret_previous="",
    assistant_google_play_package_name=PACKAGE,
    assistant_google_play_product_id=PRODUCT,
    assistant_google_play_service_account_json=service_account_json(),
)


class Db:
    def __init__(self) -> None:
        url = TEST_DB_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
        self.engine = create_async_engine(url, poolclass=NullPool)
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        self.store = SqlAssistantPaymentStore(self.sessions)
        self.reports = SqlAssistantReportStore(self.sessions)

    async def reset(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.drop_all(sync, tables=TABLES[1:]))
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=TABLES))
            await connection.execute(text("delete from users where email like 'play-%@test.local'"))

    async def user(self) -> uuid.UUID:
        user_id = uuid.uuid4()
        async with self.sessions() as session:
            session.add(User(id=user_id, email=f"play-{user_id}@test.local", password_hash="x"))
            await session.commit()
        return user_id

    async def access(self, user_id: uuid.UUID) -> AssistantAccess | None:
        async with self.sessions() as session:
            return await session.get(AssistantAccess, user_id)

    async def purchases(self) -> list[AssistantPlayPurchase]:
        async with self.sessions() as session:
            return list((await session.execute(select(AssistantPlayPurchase))).scalars())

    async def run(self, sql: str, **values: Any) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(text(sql), values)

    async def other_connections(self) -> int:
        async with self.engine.connect() as connection:
            result = await connection.execute(
                text("select count(*) from pg_stat_activity where datname = current_database() and pid <> pg_backend_pid()")
            )
            return int(result.scalar_one())


def scenario(check: Callable[[Db], Awaitable[None]]) -> None:
    async def main() -> None:
        db = Db()
        try:
            await db.reset()
            await check(db)
        finally:
            await db.engine.dispose()

    asyncio.run(main())


def new_purchase(user_id: uuid.UUID, token: str | None = None, **changes: Any) -> NewPlayPurchase:
    values: dict[str, Any] = {
        "user_id": user_id,
        "purchase_token": token or f"token-{uuid.uuid4().hex}",
        "product_id": PRODUCT,
        "google_order_id": "GPA.3301-1111-2222-33333",
        "pass_days": 30,
        "pass_tokens": TOKENS,
        "region_code": "IN",
        "purchase_time": datetime(2026, 11, 5, tzinfo=timezone.utc),
        "terms_version": "2026-11-05",
        "terms_accepted_at": datetime(2026, 11, 5, 10, 15, tzinfo=timezone.utc),
        "acknowledged": False,
        **changes,
    }
    return NewPlayPurchase(**values)


# ---- the migrations ------------------------------------------------------------------------


def test_the_migrations_build_tables_the_models_can_use_with_rls_on_and_can_run_again() -> None:
    async def check(db: Db) -> None:
        await db.run("drop table if exists assistant_play_purchases, assistant_reports")
        await db.run(
            "do $$ begin if not exists (select 1 from pg_roles where rolname = 'anon') then create role anon; end if;"
            " if not exists (select 1 from pg_roles where rolname = 'authenticated') then create role authenticated; end if; end $$"
        )
        for _ in range(2):
            for path in NEW_MIGRATIONS:
                async with db.engine.connect() as connection:
                    raw = await (await connection.execution_options(isolation_level="AUTOCOMMIT")).get_raw_connection()
                    await raw.driver_connection.execute(path.read_text(encoding="utf-8"))
        user_id = await db.user()
        assert await db.store.claim_play_purchase(new_purchase(user_id))
        report_id = await db.reports.create(NewAssistantReport(user_id, "harmful", None, "reply", None, None, "groq", "m"))
        assert report_id is not None
        async with db.engine.connect() as connection:
            rls = await connection.execute(
                text("select relname, relrowsecurity from pg_class where relname in ('assistant_play_purchases', 'assistant_reports') order by 1")
            )
            assert [tuple(row) for row in rls] == [("assistant_play_purchases", True), ("assistant_reports", True)]
            with pytest.raises(IntegrityError):
                await connection.execute(text("insert into assistant_reports (reason, reply_text) values ('spam', 'x')"))

    scenario(check)


# ---- claiming a token ----------------------------------------------------------------------


def test_a_token_grants_once_and_a_second_claim_does_nothing() -> None:
    async def check(db: Db) -> None:
        user_id = await db.user()
        purchase = new_purchase(user_id)
        assert await db.store.claim_play_purchase(purchase) is True
        assert await db.store.claim_play_purchase(purchase) is False
        access = await db.access(user_id)
        assert access is not None and access.tokens_limit == TOKENS and access.tokens_used == 0
        assert timedelta(days=29, hours=23) < access.expires_at - datetime.now(timezone.utc) <= timedelta(days=30)
        [row] = await db.purchases()
        assert (row.google_order_id, row.region_code, row.terms_version, row.status, row.acknowledged_at) == (
            "GPA.3301-1111-2222-33333",
            "IN",
            "2026-11-05",
            "granted",
            None,
        )

    scenario(check)


def test_many_claims_of_one_token_at_once_grant_exactly_once() -> None:
    async def check(db: Db) -> None:
        user_id = await db.user()
        purchase = new_purchase(user_id)
        results = await asyncio.gather(*(db.store.claim_play_purchase(purchase) for _ in range(10)))
        assert results.count(True) == 1
        access = await db.access(user_id)
        assert access is not None and access.tokens_limit == TOKENS
        assert len(await db.purchases()) == 1
        assert await db.other_connections() == 0

    scenario(check)


def test_buying_while_a_pass_runs_carries_unused_tokens_and_days_over() -> None:
    async def check(db: Db) -> None:
        user_id = await db.user()
        await db.store.claim_play_purchase(new_purchase(user_id))
        await db.run("update assistant_access set tokens_used = 500000 where user_id = :u", u=user_id)
        first = await db.access(user_id)
        assert first is not None
        await db.store.claim_play_purchase(new_purchase(user_id))
        after = await db.access(user_id)
        assert after is not None
        assert (after.tokens_limit, after.tokens_used) == (TOKENS - 500_000 + TOKENS, 0)
        assert after.expires_at - first.expires_at == timedelta(days=30)
        assert after.period_started_at == first.period_started_at

    scenario(check)


def test_buying_after_a_pass_lapsed_starts_fresh() -> None:
    async def check(db: Db) -> None:
        user_id = await db.user()
        await db.store.claim_play_purchase(new_purchase(user_id))
        await db.run("update assistant_access set expires_at = now() - interval '1 day', tokens_used = 5 where user_id = :u", u=user_id)
        await db.store.claim_play_purchase(new_purchase(user_id))
        access = await db.access(user_id)
        assert access is not None and (access.tokens_limit, access.tokens_used) == (TOKENS, 0)
        assert access.expires_at > datetime.now(timezone.utc) + timedelta(days=29)

    scenario(check)


def test_razorpay_and_google_play_passes_stack_through_the_same_grant() -> None:
    async def check(db: Db) -> None:
        user_id = await db.user()
        order_id = f"order_{uuid.uuid4().hex}"
        await db.store.save_order(NewOrder(user_id, order_id, 29900, "INR", 30, TOKENS))
        assert await db.store.grant(order_id, "pay_1") is True
        await db.store.claim_play_purchase(new_purchase(user_id))
        access = await db.access(user_id)
        assert access is not None and access.tokens_limit == 2 * TOKENS
        assert access.expires_at - datetime.now(timezone.utc) > timedelta(days=59)

    scenario(check)


def test_an_acknowledged_purchase_is_recorded_once() -> None:
    async def check(db: Db) -> None:
        user_id = await db.user()
        purchase = new_purchase(user_id)
        await db.store.claim_play_purchase(purchase)
        assert await db.store.find_play_purchase(purchase.purchase_token) == PlayPurchaseRecord(user_id, False)
        await db.store.mark_play_acknowledged(purchase.purchase_token)
        [row] = await db.purchases()
        first = row.acknowledged_at
        await db.store.mark_play_acknowledged(purchase.purchase_token)
        [row] = await db.purchases()
        assert first is not None and row.acknowledged_at == first
        record = await db.store.find_play_purchase(purchase.purchase_token)
        assert record is not None and record.acknowledged is True
        assert await db.store.find_play_purchase("unknown") is None

    scenario(check)


def test_a_consumed_purchase_is_saved_as_already_acknowledged() -> None:
    async def check(db: Db) -> None:
        user_id = await db.user()
        await db.store.claim_play_purchase(new_purchase(user_id, acknowledged=True))
        [row] = await db.purchases()
        assert row.acknowledged_at is not None

    scenario(check)


def test_deleting_an_account_keeps_its_purchases_unlinked() -> None:
    async def check(db: Db) -> None:
        user_id = await db.user()
        purchase = new_purchase(user_id)
        await db.store.claim_play_purchase(purchase)
        await db.run("delete from users where id = :u", u=user_id)
        [row] = await db.purchases()
        assert row.user_id is None
        record = await db.store.find_play_purchase(purchase.purchase_token)
        assert record is not None and record.user_id is None

    scenario(check)


# ---- the whole verify path -----------------------------------------------------------------


class FakeGoogle:
    """Google at the network: the token endpoint, the purchase and the acknowledge call."""

    def __init__(self, owner: uuid.UUID) -> None:
        self.owner = owner
        self.acknowledged: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URI:
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3599})
        if request.method == "POST":
            self.acknowledged.append(request.url.path)
            return httpx.Response(204)
        return httpx.Response(
            200,
            json={
                "purchaseState": 0,
                "consumptionState": 0,
                "acknowledgementState": 0,
                "orderId": "GPA.3301-1111-2222-33333",
                "obfuscatedExternalAccountId": str(self.owner),
                "regionCode": "IN",
                "purchaseTimeMillis": "1793873762000",
                "purchaseType": 0,
            },
        )


def test_verify_end_to_end_grants_acknowledges_and_stays_idempotent() -> None:
    async def check(db: Db) -> None:
        buyer, other = await db.user(), await db.user()
        google = FakeGoogle(buyer)
        client = HttpGooglePlayClient(SETTINGS, transport=httpx.MockTransport(google.handle))  # type: ignore[arg-type]
        access = AssistantPaymentService(SETTINGS, db.store, None, OrderRateLimiter())  # type: ignore[arg-type]
        play = GooglePlayPaymentService(SETTINGS, db.store, client, access, UserRateLimiter(10, 60.0))  # type: ignore[arg-type]
        current = {"user": buyer}
        app = FastAPI()
        app.include_router(payment_router)
        register_error_handlers(app)
        app.dependency_overrides[get_google_play_payment_service] = lambda: play
        app.dependency_overrides[get_assistant_user_id] = lambda: current["user"]
        body = {"product_id": PRODUCT, "purchase_token": "real-looking.token_123", "terms_version": "2026-11-05", "terms_accepted_at": "2026-11-05T10:15:30.000Z"}
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
                first = await api.post("/api/assistant/payments/google-play/verify", json=body)
                assert first.status_code == 200, first.text
                assert first.json()["access"]["is_prime"] is True and first.json()["access"]["active"] is True
                again = await api.post("/api/assistant/payments/google-play/verify", json=body)
                assert again.status_code == 200
                current["user"] = other
                stolen = await api.post("/api/assistant/payments/google-play/verify", json=body)
                assert stolen.status_code == 409
        finally:
            await client.aclose()
        [row] = await db.purchases()
        assert row.user_id == buyer and row.acknowledged_at is not None
        assert row.terms_accepted_at == datetime(2026, 11, 5, 10, 15, 30, tzinfo=timezone.utc)
        assert len(google.acknowledged) == 1
        assert await db.access(other) is None
        assert await db.other_connections() == 0

    scenario(check)


# ---- reports -------------------------------------------------------------------------------


def test_the_latest_completed_answer_names_the_provider_that_really_replied() -> None:
    async def check(db: Db) -> None:
        user_id, other = await db.user(), await db.user()
        assert await db.reports.latest_answer(user_id) is None
        now = datetime.now(timezone.utc)
        rows = [
            (user_id, "anthropic", "claude-haiku-5-5", "completed", now - timedelta(minutes=5)),
            (user_id, "groq", "openai/gpt-oss-120b", "completed", now - timedelta(minutes=1)),
            (user_id, "anthropic", "claude-haiku-5-5", "provider_error", now),
            (other, "anthropic", "claude-haiku-5-5", "completed", now + timedelta(minutes=1)),
        ]
        async with db.sessions() as session:
            for user, provider, model, outcome, created_at in rows:
                session.add(AssistantCallLog(user_id=user, provider=provider, model=model, mode="ask", outcome=outcome, created_at=created_at))
            await session.commit()
        answered = await db.reports.latest_answer(user_id)
        assert answered is not None and (answered.provider, answered.model) == ("groq", "openai/gpt-oss-120b")

    scenario(check)


def test_reports_list_newest_first_with_the_reporters_email_and_resolve_once() -> None:
    async def check(db: Db) -> None:
        user_id, admin, second_admin = await db.user(), await db.user(), await db.user()
        first = await db.reports.create(NewAssistantReport(user_id, "harmful", "d", "reply one", "q", "m1", "anthropic", "claude-haiku-5-5"))
        second = await db.reports.create(NewAssistantReport(user_id, "privacy", None, "reply two", None, None, "anthropic", "claude-haiku-5-5"))
        listed = await db.reports.list_by_status("open", 50)
        assert [report.id for report in listed] == [second, first]
        assert listed[0].reporter_email == f"play-{user_id}@test.local" and listed[0].model == "claude-haiku-5-5"
        resolved = await db.reports.resolve(first, admin)
        assert resolved is not None and resolved.status == "resolved" and resolved.resolved_by == admin
        again = await db.reports.resolve(first, second_admin)
        assert again is not None and again.resolved_by == admin and again.resolved_at == resolved.resolved_at
        assert [report.id for report in await db.reports.list_by_status("open", 50)] == [second]
        assert await db.reports.resolve(uuid.uuid4(), admin) is None
        await db.run("delete from users where id = :u", u=user_id)
        [kept] = await db.reports.list_by_status("open", 50)
        assert kept.user_id is None and kept.reporter_email is None

    scenario(check)
