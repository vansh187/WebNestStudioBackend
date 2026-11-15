"""Shared fakes for the VS Code extension tests: an in-memory session store with the SQL
store's rules (test_extension_sql.py checks those rules against a real Postgres), a pass
store, a usage store with token totals, a controllable clock, settings built from the real
Settings defaults, and one app (World) with everything a VS Code user touches."""

import base64
import hashlib
import os
import secrets
import uuid
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

os.environ.setdefault("SUPABASE_URL", "postgresql://user:pass@localhost:5432/test")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic_core import PydanticUndefined

from api.assistant_report_router import admin_router as report_admin_router
from api.assistant_router import router as assistant_router
from api.auth_router import router as auth_router
from api.extension_router import router as extension_router
from core.config import Settings
from core.dependencies import (
    get_app_settings,
    get_assistant_report_service,
    get_assistant_service,
    get_auth_service,
    get_db_session,
    get_extension_auth_service,
    get_extension_chat_service,
    get_extension_rate_limits,
)
from core.error_handlers import register_error_handlers
from core.exceptions import DatabaseError
from core.extension_http import ExtensionRequestIdMiddleware
from core.security import JWTHandler
from database.assistant_payment_persistence import AccessState
from database.assistant_usage_persistence import TokenTotals
from database.extension_session_persistence import (
    ConsumedCode,
    NewAuthCode,
    NewSession,
    OpenResult,
    RefreshResult,
    RevokeReason,
    SessionSummary,
    SessionUser,
)
from services.assistant_llm import FinishedEvent, StreamOpened, TextDelta, UsageEvent
from services.assistant_payment_service import AssistantPaymentService, OrderRateLimiter
from services.assistant_service import ActiveStreamRegistry, AssistantService
from services.auth_service import AuthService
from services.extension_auth_service import ExtensionAuthService
from services.extension_chat import ExtensionChatService
from services.extension_rate_limits import ExtensionRateLimits
from tests.test_assistant import FakeProvider, FakeStore
from tests.test_assistant_payments import FakeRazorpay

EXTENSION_ID = "webneststudio.webnest-ai"
REDIRECT = f"vscode://{EXTENSION_ID}/auth?windowId=1"
CLIENT = {"X-WebNest-Client": "vscode/0.1.0 (Visual Studio Code)"}
WEB_TOKEN = "website-access-token"
# RFC 7636 appendix B.
RFC_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
RFC_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
START = datetime(2026, 11, 15, 9, 30, tzinfo=timezone.utc)
# An Ask reply: text only.
TEXT_EVENTS: list[Any] = [
    StreamOpened(),
    TextDelta("`Item` has no `cost` field"),
    TextDelta("; the price is stored in `price`."),
    UsageEvent(input_tokens=812, output_tokens=96),
    FinishedEvent("stop"),
]


def ext_settings(**changes: Any) -> SimpleNamespace:
    """Every real setting with its real default, so a misspelt name fails here, then what
    these tests need: the assistant on Groq with a fake key and the extension switched on."""
    values: dict[str, Any] = {
        name: info.default for name, info in Settings.model_fields.items() if info.default is not PydanticUndefined
    }
    values.update(
        supabase_url="postgresql://user:pass@localhost:5432/test",
        jwt_secret_key="test-secret-for-the-extension-tests-0123456789",
        assistant_enabled=True,
        assistant_provider="groq",
        assistant_groq_api_key="test-key-placeholder",
        assistant_anthropic_api_key="",
        assistant_global_daily_tokens=10_000_000,
        assistant_timeout_seconds=5.0,
        extension_enabled=True,
    )
    values.update(changes)
    return SimpleNamespace(**values)


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class Clock:
    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@dataclass
class FakeUser:
    email: str
    full_name: str | None = "Asha Verma"
    role: str = "client"
    is_active: bool = True


@dataclass
class _Code:
    user_id: uuid.UUID
    code_challenge: str
    redirect_uri: str
    device_name: str | None
    app_name: str | None
    ext_version: str | None
    expires_at: datetime
    used_at: datetime | None = None


@dataclass
class _Session:
    id: uuid.UUID
    user_id: uuid.UUID
    device_name: str | None
    app_name: str | None
    ext_version: str | None
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    revoked_reason: str | None = None


@dataclass
class _Token:
    session_id: uuid.UUID
    expires_at: datetime
    used_at: datetime | None = None


@dataclass
class FakeSessionStore:
    users: dict[uuid.UUID, FakeUser] = field(default_factory=dict)
    codes: dict[str, _Code] = field(default_factory=dict)
    sessions: dict[uuid.UUID, _Session] = field(default_factory=dict)
    tokens: dict[str, _Token] = field(default_factory=dict)
    fail: bool = False
    calls: int = 0

    def _check(self) -> None:
        self.calls += 1
        if self.fail:
            raise DatabaseError("Could not read your VS Code session right now. Please try again shortly.")

    def add_user(self, **changes: Any) -> uuid.UUID:
        user_id = uuid.uuid4()
        self.users[user_id] = replace(FakeUser(email=f"user-{user_id.hex[:8]}@example.com"), **changes)
        return user_id

    async def create_code(self, code: NewAuthCode) -> None:
        self._check()
        self.codes[code.code_hash] = _Code(
            code.user_id, code.code_challenge, code.redirect_uri, code.device_name, code.app_name, code.ext_version, code.expires_at
        )

    async def consume_code(self, code_hash: str, now: datetime) -> ConsumedCode | None:
        self._check()
        code = self.codes.get(code_hash)
        if code is None or code.used_at is not None or code.expires_at <= now:
            return None
        code.used_at = now
        return ConsumedCode(code.user_id, code.code_challenge, code.redirect_uri, code.device_name, code.app_name, code.ext_version)

    async def open_session(self, new: NewSession, now: datetime) -> OpenResult:
        self._check()
        user = self.users.get(new.user_id)
        if user is None:
            return OpenResult("missing")
        if not user.is_active:
            return OpenResult("disabled")
        session = _Session(uuid.uuid4(), new.user_id, new.device_name, new.app_name, new.ext_version, now, now, new.expires_at)
        self.sessions[session.id] = session
        self.tokens[new.token_hash] = _Token(session.id, new.token_expires_at)
        return OpenResult("ok", session.id, user.role)

    async def refresh(
        self,
        old_hash: str,
        new_hash: str,
        now: datetime,
        grace: timedelta,
        token_lifetime: timedelta,
        ext_version: str | None,
    ) -> RefreshResult:
        self._check()
        token = self.tokens.get(old_hash)
        if token is None:
            return RefreshResult("unknown")
        if token.used_at is None and token.expires_at > now:
            token.used_at = now
        elif token.used_at is None:
            return RefreshResult("expired", token.session_id)
        elif now - token.used_at > grace:
            session = self.sessions[token.session_id]
            if session.revoked_at is None:
                session.revoked_at, session.revoked_reason = now, "reuse"
            return RefreshResult("reused", token.session_id)
        session = self.sessions[token.session_id]
        user = self.users[session.user_id]
        if not user.is_active:
            return RefreshResult("disabled", session.id, session.user_id)
        if session.revoked_at is not None:
            return RefreshResult("revoked", session.id, session.user_id)
        if session.expires_at <= now:
            return RefreshResult("expired", session.id, session.user_id)
        expires_at = min(session.expires_at, now + token_lifetime)
        self.tokens[new_hash] = _Token(session.id, expires_at)
        session.last_used_at = now
        session.ext_version = ext_version or session.ext_version
        return RefreshResult("ok", session.id, session.user_id, user.role, expires_at)

    async def session_user(self, session_id: uuid.UUID) -> SessionUser | None:
        self._check()
        session = self.sessions.get(session_id)
        if session is None:
            return None
        user = self.users[session.user_id]
        return SessionUser(
            session.id, session.user_id, user.email, user.full_name, user.role, user.is_active, session.revoked_at is not None, session.expires_at
        )

    async def revoke_by_token(self, token_hash: str, now: datetime, reason: RevokeReason) -> bool:
        self._check()
        token = self.tokens.get(token_hash)
        if token is None:
            return False
        session = self.sessions[token.session_id]
        if session.revoked_at is not None:
            return False
        session.revoked_at, session.revoked_reason = now, reason
        return True

    async def revoke_session(self, user_id: uuid.UUID, session_id: uuid.UUID, now: datetime, reason: RevokeReason) -> bool:
        self._check()
        session = self.sessions.get(session_id)
        if session is None or session.user_id != user_id or session.revoked_at is not None or session.expires_at <= now:
            return False
        session.revoked_at, session.revoked_reason = now, reason
        return True

    async def revoke_all_for_user(self, user_id: uuid.UUID, now: datetime, reason: RevokeReason) -> int:
        self._check()
        count = 0
        for session in self.sessions.values():
            if session.user_id == user_id and session.revoked_at is None:
                session.revoked_at, session.revoked_reason = now, reason
                count += 1
        return count

    async def list_sessions(self, user_id: uuid.UUID, now: datetime) -> list[SessionSummary]:
        self._check()
        live = [s for s in self.sessions.values() if s.user_id == user_id and s.revoked_at is None and s.expires_at > now]
        live.sort(key=lambda s: (-s.last_used_at.timestamp(), str(s.id)))
        return [SessionSummary(s.id, s.device_name, s.app_name, s.ext_version, s.created_at, s.last_used_at, s.expires_at) for s in live]

    async def purge(self, codes_and_tokens_before: datetime, sessions_before: datetime) -> int:
        self._check()
        doomed_codes = [key for key, code in self.codes.items() if code.expires_at < codes_and_tokens_before]
        doomed_sessions = [
            key
            for key, session in self.sessions.items()
            if (session.revoked_at is not None and session.revoked_at < sessions_before) or session.expires_at < sessions_before
        ]
        doomed_tokens = [
            key for key, token in self.tokens.items() if token.expires_at < codes_and_tokens_before or token.session_id in doomed_sessions
        ]
        for key in doomed_codes:
            del self.codes[key]
        for key in doomed_tokens:
            del self.tokens[key]
        for key in doomed_sessions:
            del self.sessions[key]
        return len(doomed_codes) + len(doomed_tokens) + len(doomed_sessions)


class TotalsStore(FakeStore):
    """test_assistant's usage fake, plus the meters /me reads."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.today_total = 0
        self.week_total = 0
        self.fail_totals = False

    async def token_totals(self, user_id: uuid.UUID, day: date) -> TokenTotals:
        if self.fail_totals:
            raise DatabaseError("Could not read assistant usage right now. Please try again shortly.")
        return TokenTotals(today=self.today_total, week=self.week_total)


class FakePassStore:
    """The Prime pass, per user, with the SQL store's reserve rule."""

    def __init__(self) -> None:
        self.access: dict[uuid.UUID, AccessState] = {}
        self.reserve_calls = 0

    def give(self, user_id: uuid.UUID, days: float = 20, limit: int = 2_000_000, used: int = 0) -> None:
        self.access[user_id] = AccessState(datetime.now(timezone.utc) + timedelta(days=days), limit, used)

    async def get_access(self, user_id: uuid.UUID) -> AccessState | None:
        return self.access.get(user_id)

    async def reserve_tokens(self, user_id: uuid.UUID, tokens: int) -> bool:
        self.reserve_calls += 1
        state = self.access.get(user_id)
        if state is None or state.expires_at <= datetime.now(timezone.utc) or state.tokens_used >= state.tokens_limit:
            return False
        self.access[user_id] = AccessState(state.expires_at, state.tokens_limit, state.tokens_used + tokens)
        return True

    async def add_tokens(self, user_id: uuid.UUID, tokens: int) -> AccessState | None:
        state = self.access.get(user_id)
        if state is None:
            return None
        self.access[user_id] = AccessState(state.expires_at, state.tokens_limit, max(state.tokens_used + tokens, 0))
        return self.access[user_id]


class FakeUsers:
    """The website's user lookups, from the same users as the session store."""

    def __init__(self, store: FakeSessionStore) -> None:
        self._store = store

    async def get_by_id(self, user_id: uuid.UUID) -> Any:
        user = self._store.users.get(user_id)
        if user is None:
            return None
        return SimpleNamespace(
            id=user_id,
            email=user.email,
            full_name=user.full_name,
            role=user.role,
            is_active=user.is_active,
            is_verified=True,
            phone_number=None,
            created_at=START,
            last_login_at=None,
        )


class FakeDbSession:
    def __init__(self) -> None:
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


class FakeReports:
    def __init__(self) -> None:
        self.reports: list[tuple[uuid.UUID, Any]] = []

    async def report(self, user_id: uuid.UUID, payload: Any) -> Any:
        self.reports.append((user_id, payload))
        return {"id": str(uuid.uuid4()), "status": "open"}


class World:
    """One app with the real extension router, error handlers and middleware, the real auth,
    chat and assistant services, and the website's real login check; only the database and
    the AI provider are faked. Also mounts /api/assistant/*, /api/auth/me and an admin route,
    to check what a VS Code token can't reach."""

    def __init__(self, events: list[Any] | None = None, paywall: bool = True, provider: FakeProvider | None = None, **changes: Any) -> None:
        self.settings = ext_settings(assistant_paywall_enabled=paywall, **changes)
        self.clock = Clock(datetime.now(timezone.utc).replace(microsecond=0))
        self.store = FakeSessionStore()
        self.user = self.store.add_user(email="asha@example.com", full_name="Asha Verma")
        self.jwt = JWTHandler(self.settings)  # type: ignore[arg-type]
        self.auth = ExtensionAuthService(self.settings, self.store, self.jwt, self.clock)  # type: ignore[arg-type]
        self.provider = provider or FakeProvider(TEXT_EVENTS if events is None else events)
        self.usage = TotalsStore()
        self.registry = ActiveStreamRegistry()
        self.passes = FakePassStore()
        self.payments = AssistantPaymentService(self.settings, self.passes, FakeRazorpay(), OrderRateLimiter())  # type: ignore[arg-type]
        self.assistant = AssistantService(
            self.settings, self.provider, self.usage, self.registry, payments=self.payments  # type: ignore[arg-type]
        )
        self.chat_service = ExtensionChatService(self.settings, self.assistant, self.payments, self.usage)  # type: ignore[arg-type]
        self.limits = ExtensionRateLimits()
        self.reports = FakeReports()
        self.db_session = FakeDbSession()
        self.web_auth = AuthService(None, self.settings, None, self.jwt, None)  # type: ignore[arg-type]
        self.web_auth._users = FakeUsers(self.store)  # type: ignore[assignment]

        app = FastAPI()
        app.add_middleware(ExtensionRequestIdMiddleware)
        for router in (extension_router, assistant_router, auth_router, report_admin_router):
            app.include_router(router)
        register_error_handlers(app)
        overrides = app.dependency_overrides
        overrides[get_app_settings] = lambda: self.settings
        overrides[get_extension_auth_service] = lambda: self.auth
        overrides[get_extension_chat_service] = lambda: self.chat_service
        overrides[get_extension_rate_limits] = lambda: self.limits
        overrides[get_assistant_report_service] = lambda: self.reports
        overrides[get_assistant_service] = lambda: self.assistant
        overrides[get_auth_service] = lambda: self.web_auth
        overrides[get_db_session] = lambda: self.db_session
        self.app = app
        self.client = TestClient(app, raise_server_exceptions=False)

    def web_token(self, user_id: uuid.UUID | None = None) -> str:
        """A real website access token, as the website's login issues it."""
        return self.jwt.create_access_token(str(user_id or self.user), "client")

    def web_headers(self, user_id: uuid.UUID | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.web_token(user_id)}"}

    def code_body(self, challenge: str, **changes: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "redirect_uri": REDIRECT,
            "device_name": "VANSH-LAPTOP (win32)",
            "app_scheme": "vscode",
            "ext_version": "0.1.0",
        }
        body.update(changes)
        return body

    def login(self, user_id: uuid.UUID | None = None, **code_changes: Any) -> dict[str, Any]:
        """The whole browser login: the website asks for a code, VS Code swaps it."""
        verifier, challenge = pkce_pair()
        created = self.client.post(
            "/api/extension/auth/codes", json=self.code_body(challenge, **code_changes), headers=self.web_headers(user_id)
        )
        assert created.status_code == 201, created.text
        redirect = code_changes.get("redirect_uri", REDIRECT)
        exchanged = self.client.post(
            "/api/extension/auth/token",
            json={"code": created.json()["code"], "code_verifier": verifier, "redirect_uri": redirect},
            headers=CLIENT,
        )
        assert exchanged.status_code == 200, exchanged.text
        return exchanged.json()

    def headers(self, tokens: dict[str, Any], **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {tokens['access_token']}", **CLIENT, **extra}

    def give_prime(self, user_id: uuid.UUID | None = None, **changes: Any) -> None:
        self.passes.give(user_id or self.user, **changes)


def chat_body(text: str = "Why does this fail?", context: dict[str, Any] | None = None, **changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "conversation_id": "1b4e28ba-2fa1-11d2-883f-0016d3cca427",
        "client_request_id": "9a8b7c6d-5e4f-4a3b-2c1d-0e9f8a7b6c5d",
        "mode": "ask",
        "approval": "manual",
        "phase": "execute",
        "messages": [{"role": "user", "content": text}],
        "context": context if context is not None else {},
    }
    body.update(changes)
    return body


def sent_user_text(provider: FakeProvider) -> str:
    """The newest user message the provider was sent."""
    messages = provider.requests[-1].messages
    return next(message["content"] for message in reversed(messages) if message["role"] == "user")
