"""POST /api/assistant/reports and the admin review endpoints, through the real routers with an
in-memory store. Reporting an AI reply is what Google Play's AI-generated content policy asks for."""

import json
import logging
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.assistant_report_router import admin_router, router
from api.assistant_router import get_assistant_user_id
from core.dependencies import get_assistant_report_service, get_current_user
from core.error_handlers import register_error_handlers
from database.assistant_report_persistence import AnsweredBy, NewAssistantReport, SavedAssistantReport
from services.assistant_report_service import AssistantReportService
from services.user_rate_limiter import UserRateLimiter

USER = uuid.uuid4()
ADMIN = uuid.uuid4()
URL = "/api/assistant/reports"


class FakeReportStore:
    def __init__(self, answered: AnsweredBy | None = None) -> None:
        self.reports: dict[uuid.UUID, SavedAssistantReport] = {}
        self.answered = answered

    async def latest_answer(self, user_id: uuid.UUID) -> AnsweredBy | None:
        return self.answered

    async def create(self, report: NewAssistantReport) -> uuid.UUID:
        report_id = uuid.uuid4()
        self.reports[report_id] = SavedAssistantReport(
            id=report_id,
            reporter_email=f"{report.user_id}@test.local",
            status="open",
            created_at=datetime.now(timezone.utc),
            resolved_at=None,
            resolved_by=None,
            **vars(report),
        )
        return report_id

    async def list_by_status(self, status: str, limit: int) -> list[SavedAssistantReport]:
        found = [report for report in self.reports.values() if report.status == status]
        return sorted(found, key=lambda report: report.created_at, reverse=True)[:limit]

    async def resolve(self, report_id: uuid.UUID, admin_id: uuid.UUID) -> SavedAssistantReport | None:
        report = self.reports.get(report_id)
        if report is None:
            return None
        if report.status == "open":
            report = SavedAssistantReport(**{**vars(report), "status": "resolved", "resolved_at": datetime.now(timezone.utc), "resolved_by": admin_id})
            self.reports[report_id] = report
        return report


class Setup:
    def __init__(
        self,
        limit: int = 20,
        provider: Any = SimpleNamespace(name="anthropic", model="claude-haiku-5-5"),
        answered: AnsweredBy | None = AnsweredBy("anthropic", "claude-haiku-5-5"),
    ) -> None:
        self.store = FakeReportStore(answered)
        self.service = AssistantReportService(self.store, UserRateLimiter(limit, 3600.0), provider)  # type: ignore[arg-type]

    def client(self, user: uuid.UUID | None = USER, role: str = "client") -> TestClient:
        app = FastAPI()
        app.include_router(router)
        app.include_router(admin_router)
        register_error_handlers(app)
        app.dependency_overrides[get_assistant_report_service] = lambda: self.service
        if user is not None:
            app.dependency_overrides[get_assistant_user_id] = lambda: user
            app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=user, role=role)
        return TestClient(app, raise_server_exceptions=False)


def body(**changes: Any) -> dict[str, Any]:
    return {
        "reason": "harmful",
        "details": "It told me to do something unsafe",
        "reply_text": "the AI reply",
        "prompt_text": "my question",
        "client_message_id": "msg-42",
        **changes,
    }


def test_login_is_required() -> None:
    assert Setup().client(user=None).post(URL, json=body()).status_code == 401


def test_a_report_is_saved_with_the_model_that_was_in_use() -> None:
    setup = Setup()
    response = setup.client().post(URL, json=body())
    assert response.status_code == 201
    created = response.json()
    assert created["status"] == "open"
    saved = setup.store.reports[uuid.UUID(created["id"])]
    assert (saved.user_id, saved.reason, saved.reply_text, saved.prompt_text) == (USER, "harmful", "the AI reply", "my question")
    assert (saved.provider_name, saved.model) == ("anthropic", "claude-haiku-5-5")


def test_the_report_names_the_model_that_really_answered_not_the_configured_one() -> None:
    setup = Setup(answered=AnsweredBy("groq", "openai/gpt-oss-120b"))
    assert setup.client().post(URL, json=body()).status_code == 201
    saved = next(iter(setup.store.reports.values()))
    assert (saved.provider_name, saved.model) == ("groq", "openai/gpt-oss-120b")


def test_with_no_logged_answer_the_configured_provider_stands_in() -> None:
    setup = Setup(answered=None)
    setup.client().post(URL, json=body())
    saved = next(iter(setup.store.reports.values()))
    assert (saved.provider_name, saved.model) == ("anthropic", "claude-haiku-5-5")


def test_with_no_logged_answer_and_no_provider_a_report_still_saves() -> None:
    setup = Setup(provider=None, answered=None)
    assert setup.client().post(URL, json=body()).status_code == 201
    saved = next(iter(setup.store.reports.values()))
    assert (saved.provider_name, saved.model) == (None, None)


@pytest.mark.parametrize("reason", ["spam", "", "HARMFUL", None, 3])
def test_an_unknown_reason_is_a_422(reason: Any) -> None:
    assert Setup().client().post(URL, json=body(reason=reason)).status_code == 422


@pytest.mark.parametrize("reply", [None, "", "   \n"])
def test_a_report_needs_the_reply_text(reply: Any) -> None:
    payload = body(reply_text=reply) if reply is not None else {key: value for key, value in body().items() if key != "reply_text"}
    assert Setup().client().post(URL, json=payload).status_code == 422


def test_only_the_reason_and_reply_are_required() -> None:
    assert Setup().client().post(URL, json={"reason": "other", "reply_text": "text"}).status_code == 201


def test_long_fields_are_cut_not_refused() -> None:
    setup = Setup()
    payload = body(details="d" * 5000, reply_text="r" * 9000, prompt_text="p" * 5000, client_message_id="m" * 200)
    assert setup.client().post(URL, json=payload).status_code == 201
    saved = next(iter(setup.store.reports.values()))
    assert (len(saved.details or ""), len(saved.reply_text), len(saved.prompt_text or ""), len(saved.client_message_id or "")) == (1000, 8000, 4000, 64)


def test_nul_bytes_are_removed_since_postgres_cannot_store_them() -> None:
    setup = Setup()
    assert setup.client().post(URL, json=body(reply_text="bad\x00reply")).status_code == 201
    assert next(iter(setup.store.reports.values())).reply_text == "badreply"


def test_a_long_reply_with_escaped_characters_is_cut_not_refused() -> None:
    setup = Setup()
    # Each emoji is sent as a 12-byte 😀 escape: about 400 KB on the wire.
    reply = "\U0001F600" * 35_000
    response = setup.client().post(URL, content=json.dumps(body(reply_text=reply)), headers={"content-type": "application/json"})
    assert response.status_code == 201
    assert next(iter(setup.store.reports.values())).reply_text == "\U0001F600" * 8000


def test_an_abusive_body_is_a_413_even_before_it_is_read() -> None:
    client = Setup().client()
    assert client.post(URL, json=body(reply_text="r" * 1_100_000)).status_code == 413
    declared = client.post(URL, content=b"{}", headers={"content-type": "application/json", "content-length": str(2 * 1024 * 1024)})
    assert declared.status_code == 413


def test_validation_errors_do_not_echo_the_reported_text() -> None:
    response = Setup().client().post(URL, json=body(reason="spam", reply_text="very private words"))
    assert response.status_code == 422
    assert "very private words" not in response.text


def test_more_than_twenty_reports_an_hour_is_a_429() -> None:
    client = Setup().client()
    statuses = [client.post(URL, json=body()).status_code for _ in range(21)]
    assert statuses == [201] * 20 + [429]


def test_logs_never_hold_the_reported_text(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    Setup().client().post(URL, json=body(reply_text="secret reply words", prompt_text="secret prompt words", details="secret details"))
    assert "AI reply reported" in caplog.text
    assert "secret" not in caplog.text


# ---- admin review -------------------------------------------------------------------------


def test_non_admins_can_not_list_or_resolve() -> None:
    setup = Setup()
    client = setup.client()
    report_id = client.post(URL, json=body()).json()["id"]
    assert client.get("/api/admin/assistant-reports").status_code == 403
    assert client.post(f"/api/admin/assistant-reports/{report_id}/resolve").status_code == 403
    assert setup.store.reports[uuid.UUID(report_id)].status == "open"


def test_admins_list_open_reports_newest_first_with_the_reporters_email() -> None:
    setup = Setup()
    reporter = setup.client()
    first = reporter.post(URL, json=body(reason="harmful")).json()["id"]
    second = reporter.post(URL, json=body(reason="misleading")).json()["id"]
    listed = setup.client(user=ADMIN, role="admin").get("/api/admin/assistant-reports").json()["reports"]
    assert [report["id"] for report in listed] == [second, first]
    assert listed[0]["reporter_email"] == f"{USER}@test.local"
    assert listed[0]["model"] == "claude-haiku-5-5"


def test_admins_resolve_a_report_and_it_moves_to_resolved() -> None:
    setup = Setup()
    report_id = setup.client().post(URL, json=body()).json()["id"]
    admin = setup.client(user=ADMIN, role="admin")
    resolved = admin.post(f"/api/admin/assistant-reports/{report_id}/resolve")
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "resolved" and resolved.json()["resolved_by"] == str(ADMIN)
    assert admin.get("/api/admin/assistant-reports?status=open").json()["reports"] == []
    assert [report["id"] for report in admin.get("/api/admin/assistant-reports?status=resolved").json()["reports"]] == [report_id]


def test_resolving_twice_keeps_the_first_resolution() -> None:
    setup = Setup()
    report_id = setup.client().post(URL, json=body()).json()["id"]
    first = setup.client(user=ADMIN, role="admin").post(f"/api/admin/assistant-reports/{report_id}/resolve").json()
    other_admin = uuid.uuid4()
    second = setup.client(user=other_admin, role="admin").post(f"/api/admin/assistant-reports/{report_id}/resolve").json()
    assert second["resolved_by"] == first["resolved_by"] == str(ADMIN)


def test_resolving_an_unknown_report_is_a_404() -> None:
    assert Setup().client(user=ADMIN, role="admin").post(f"/api/admin/assistant-reports/{uuid.uuid4()}/resolve").status_code == 404


def test_an_unknown_status_filter_is_a_422() -> None:
    assert Setup().client(user=ADMIN, role="admin").get("/api/admin/assistant-reports?status=all").status_code == 422
