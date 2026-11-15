"""WebNest AI for VS Code over HTTP: the real router, middleware, error handlers, auth, chat
and assistant services and the website's login check, with only the database and the AI
provider faked (tests/extension_fakes.py). Each test is a user journey, or a refusal the
extension must be able to act on."""

import asyncio
import json
import uuid
from typing import Any

import pytest

from tests.extension_fakes import CLIENT, REDIRECT, TEXT_EVENTS, World, chat_body, pkce_pair, sent_user_text

from core.exceptions import DatabaseError
from services.assistant_llm import (
    FinishedEvent,
    ProviderRateLimitedError,
    ProviderStreamError,
    ProviderUnavailableError,
    StreamOpened,
    TextDelta,
    UsageEvent,
)
from tests.test_assistant import FakeProvider, parse_sse

ERROR_KEYS = {"detail", "code", "request_id"}


def assert_error(response: Any, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code
    assert ERROR_KEYS <= set(body)
    assert response.headers["x-request-id"] == body["request_id"]
    if "retry_after" in body:
        assert response.headers["retry-after"] == str(body["retry_after"])
    return body


@pytest.fixture
def world() -> World:
    return World()


# ---- the whole journey ------------------------------------------------------------------------


def test_connect_ask_refresh_and_log_out(world: World) -> None:
    world.give_prime()
    tokens = world.login()
    assert set(tokens) == {"access_token", "access_expires_in", "refresh_token", "refresh_expires_at", "session_id", "token_type"}
    assert tokens["refresh_expires_at"].endswith("Z")

    me = world.client.get("/api/extension/me", headers=world.headers(tokens))
    assert me.status_code == 200 and me.json()["access"]["active"] is True

    reply = world.client.post("/api/extension/chat", json=chat_body(), headers=world.headers(tokens, **{"X-Request-Id": "req-12345"}))
    assert reply.status_code == 200 and reply.headers["content-type"].startswith("text/event-stream")
    assert reply.headers["x-request-id"] == "req-12345"
    events = parse_sse(reply.text)
    assert [name for name, _ in events] == ["start", "text", "text", "usage", "done"]
    assert events[0][1]["request_id"] == "req-12345"
    assert events[-1][1] == {"finish_reason": "stop"}
    assert events[3][1]["access"]["is_prime"] is True
    assert world.usage.call_log[-1].surface == "vscode"

    refreshed = world.client.post("/api/extension/auth/refresh", json={"refresh_token": tokens["refresh_token"]}, headers=CLIENT)
    assert refreshed.status_code == 200 and refreshed.json()["session_id"] == tokens["session_id"]

    out = world.client.post("/api/extension/auth/logout", json={"refresh_token": refreshed.json()["refresh_token"]})
    assert out.status_code == 204 and out.content == b""
    assert_error(world.client.get("/api/extension/me", headers=world.headers(refreshed.json())), 401, "session_revoked")


def test_every_reply_starts_with_start_and_then_the_assistant_stream_unchanged(world: World) -> None:
    world.give_prime()
    tokens = world.login()
    events = parse_sse(world.client.post("/api/extension/chat", json=chat_body(), headers=world.headers(tokens)).text)
    assert events[0][0] == "start" and events[0][1]["message_id"].startswith("msg_")
    assert "".join(data["delta"] for name, data in events if name == "text") == "`Item` has no `cost` field; the price is stored in `price`."
    usage = dict(events)["usage"]
    assert (usage["input_tokens"], usage["output_tokens"], usage["provider_name"]) == (812, 96, "WebNest AI")


def test_a_missing_or_insane_request_id_is_replaced(world: World) -> None:
    first = world.client.get("/api/extension/me")
    assert len(first.headers["x-request-id"]) == 36
    insane = world.client.get("/api/extension/me", headers={"X-Request-Id": "bad id\r\nwith<script>"})
    assert insane.headers["x-request-id"] != "bad id\r\nwith<script>" and len(insane.headers["x-request-id"]) == 36


# ---- 2.1 codes -----------------------------------------------------------------------------------


def test_the_website_gets_a_code_that_lives_five_minutes(world: World) -> None:
    _, challenge = pkce_pair()
    response = world.client.post("/api/extension/auth/codes", json=world.code_body(challenge), headers=world.web_headers())
    assert response.status_code == 201
    body = response.json()
    assert body["code"].startswith("wn_ac_") and body["expires_in"] == 300
    assert world.db_session.closed >= 1


@pytest.mark.parametrize(
    "redirect",
    [
        "https://evil.example/auth",
        "vscode://other.extension/auth",
        "vscode://webneststudio.webnest-ai/auth?windowId=1&x=2",
        "vscode://webneststudio.webnest-ai/auth#frag",
        "evilscheme://webneststudio.webnest-ai/auth",
    ],
)
def test_a_disallowed_redirect_is_400_invalid_redirect_uri(world: World, redirect: str) -> None:
    _, challenge = pkce_pair()
    response = world.client.post(
        "/api/extension/auth/codes", json=world.code_body(challenge, redirect_uri=redirect), headers=world.web_headers()
    )
    assert_error(response, 400, "invalid_redirect_uri")


def test_codes_need_the_websites_own_token(world: World) -> None:
    _, challenge = pkce_pair()
    tokens = world.login()
    for headers in ({}, {"Authorization": "Bearer nonsense"}, {"Authorization": f"Bearer {tokens['access_token']}"}):
        response = world.client.post("/api/extension/auth/codes", json=world.code_body(challenge), headers=headers)
        assert_error(response, 401, "not_authenticated")


def test_more_than_ten_codes_a_minute_is_429(world: World) -> None:
    _, challenge = pkce_pair()
    for _ in range(10):
        assert world.client.post("/api/extension/auth/codes", json=world.code_body(challenge), headers=world.web_headers()).status_code == 201
    body = assert_error(
        world.client.post("/api/extension/auth/codes", json=world.code_body(challenge), headers=world.web_headers()), 429, "rate_limited"
    )
    assert body["retry_after"] == 60


@pytest.mark.parametrize(
    "changes",
    [{"code_challenge": "short"}, {"code_challenge_method": "plain"}, {"redirect_uri": None}, {"device_name": 5}],
)
def test_a_bad_code_body_is_422_without_echoing_it(world: World, changes: dict[str, Any]) -> None:
    _, challenge = pkce_pair()
    response = world.client.post(
        "/api/extension/auth/codes", json=world.code_body(challenge, **changes), headers=world.web_headers()
    )
    body = assert_error(response, 422, "validation_error")
    assert body["detail"] == "Request validation failed"
    assert all(set(error) == {"loc", "msg"} and error["loc"][0] == "body" for error in body["errors"])


def test_app_schemes_are_listed_by_name(world: World) -> None:
    world.login(app_scheme="cursor")
    world.login(app_scheme="my-fork-of-code")
    listed = world.client.get("/api/extension/sessions", headers=world.web_headers()).json()["sessions"]
    assert sorted(session["app_name"] for session in listed) == ["Cursor", "my-fork-of-code"]


# ---- 2.3 token ----------------------------------------------------------------------------------


def exchange(world: World, code: str, verifier: str, redirect: str = REDIRECT) -> Any:
    return world.client.post("/api/extension/auth/token", json={"code": code, "code_verifier": verifier, "redirect_uri": redirect})


def new_code(world: World) -> tuple[str, str]:
    verifier, challenge = pkce_pair()
    response = world.client.post("/api/extension/auth/codes", json=world.code_body(challenge), headers=world.web_headers())
    return response.json()["code"], verifier


def test_a_code_works_once(world: World) -> None:
    code, verifier = new_code(world)
    assert exchange(world, code, verifier).status_code == 200
    assert_error(exchange(world, code, verifier), 400, "invalid_code")


def test_a_wrong_verifier_is_invalid_code_verifier_and_burns_the_code(world: World) -> None:
    code, verifier = new_code(world)
    assert_error(exchange(world, code, "x" * 43), 400, "invalid_code_verifier")
    assert_error(exchange(world, code, verifier), 400, "invalid_code")


def test_another_redirect_than_the_codes_is_invalid_redirect_uri(world: World) -> None:
    code, verifier = new_code(world)
    assert_error(exchange(world, code, verifier, REDIRECT.replace("windowId=1", "windowId=2")), 400, "invalid_redirect_uri")


def test_an_old_code_is_invalid_code(world: World) -> None:
    code, verifier = new_code(world)
    world.clock.advance(minutes=5, seconds=1)
    assert_error(exchange(world, code, verifier), 400, "invalid_code")


def test_a_disabled_account_can_not_connect(world: World) -> None:
    code, verifier = new_code(world)
    world.store.users[world.user].is_active = False
    assert_error(exchange(world, code, verifier), 403, "account_disabled")


def test_more_than_twenty_exchanges_a_minute_from_one_ip_is_429(world: World) -> None:
    for _ in range(20):
        assert exchange(world, "wn_ac_nothing", "v" * 43).status_code == 400
    assert_error(exchange(world, "wn_ac_nothing", "v" * 43), 429, "rate_limited")
    other_ip = world.client.post(
        "/api/extension/auth/token",
        json={"code": "wn_ac_nothing", "code_verifier": "v" * 43, "redirect_uri": REDIRECT},
        headers={"X-Forwarded-For": "203.0.113.9"},
    )
    assert other_ip.status_code == 400


@pytest.mark.parametrize("raw", [b"", b"not json", b"[]", b'{"code": 5}', b'{"code": "wn_ac_x", "code_verifier": "' + b"a" * 300 + b'"}'])
def test_a_bad_token_body_is_422(world: World, raw: bytes) -> None:
    response = world.client.post("/api/extension/auth/token", content=raw, headers={"Content-Type": "application/json"})
    assert_error(response, 422, "validation_error")


# ---- 2.4 refresh -------------------------------------------------------------------------------


def refresh(world: World, token: str) -> Any:
    return world.client.post("/api/extension/auth/refresh", json={"refresh_token": token}, headers=CLIENT)


def test_refresh_rotates_within_one_session_and_records_the_version(world: World) -> None:
    tokens = world.login()
    second = world.client.post(
        "/api/extension/auth/refresh", json={"refresh_token": tokens["refresh_token"]}, headers={"X-WebNest-Client": "vscode/0.2.0 (Cursor)"}
    ).json()
    assert second["session_id"] == tokens["session_id"] and second["refresh_token"] != tokens["refresh_token"]
    assert world.store.sessions[uuid.UUID(tokens["session_id"])].ext_version == "0.2.0"


def test_two_windows_refreshing_at_once_both_get_a_pair(world: World) -> None:
    tokens = world.login()
    first, second = refresh(world, tokens["refresh_token"]), refresh(world, tokens["refresh_token"])
    assert first.status_code == second.status_code == 200
    assert first.json()["session_id"] == second.json()["session_id"] == tokens["session_id"]


def test_reuse_after_30_seconds_ends_the_session(world: World) -> None:
    tokens = world.login()
    newest = refresh(world, tokens["refresh_token"]).json()
    world.clock.advance(seconds=31)
    assert_error(refresh(world, tokens["refresh_token"]), 401, "session_revoked")
    assert_error(refresh(world, newest["refresh_token"]), 401, "session_revoked")
    assert_error(world.client.get("/api/extension/me", headers=world.headers(newest)), 401, "session_revoked")


def test_an_ended_session_is_session_expired(world: World) -> None:
    tokens = world.login()
    world.clock.advance(days=31)
    assert_error(refresh(world, tokens["refresh_token"]), 401, "session_expired")


def test_a_disabled_user_refreshing_is_account_disabled(world: World) -> None:
    tokens = world.login()
    world.store.users[world.user].is_active = False
    assert_error(refresh(world, tokens["refresh_token"]), 403, "account_disabled")


def test_more_than_twenty_refreshes_a_minute_from_one_ip_is_429(world: World) -> None:
    for _ in range(20):
        refresh(world, "wn_rt_nothing")
    assert_error(refresh(world, "wn_rt_nothing"), 429, "rate_limited")


# ---- 2.5 logout --------------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [b"", b"nonsense", b'{"refresh_token": "wn_rt_unknown"}', b'{"refresh_token": 5}', b"x" * 20_000])
def test_logout_is_always_204(world: World, raw: bytes) -> None:
    response = world.client.post("/api/extension/auth/logout", content=raw, headers={"Content-Type": "application/json"})
    assert response.status_code == 204


def test_logout_is_204_even_when_the_database_is_down(world: World) -> None:
    tokens = world.login()
    world.store.fail = True
    assert world.client.post("/api/extension/auth/logout", json={"refresh_token": tokens["refresh_token"]}).status_code == 204


# ---- scope: what each token can reach -----------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [("get", "/api/auth/me"), ("post", "/api/assistant/chat"), ("get", "/api/assistant/usage"), ("get", "/api/admin/assistant-reports")],
)
def test_an_extension_token_works_nowhere_else(world: World, method: str, path: str) -> None:
    tokens = world.login()
    response = getattr(world.client, method)(path, headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert response.status_code == 401


def test_the_website_token_still_works_on_the_website(world: World) -> None:
    response = world.client.get("/api/auth/me", headers=world.web_headers())
    assert response.status_code == 200 and response.json()["email"] == "asha@example.com"


@pytest.mark.parametrize(("method", "path"), [("get", "/api/extension/me"), ("post", "/api/extension/chat"), ("post", "/api/extension/reports")])
def test_a_website_token_is_not_authenticated_on_the_extension(world: World, method: str, path: str) -> None:
    kwargs: dict[str, Any] = {"headers": {**world.web_headers(), **CLIENT}}
    if method == "post":
        kwargs["json"] = chat_body()
    assert_error(getattr(world.client, method)(path, **kwargs), 401, "not_authenticated")


@pytest.mark.parametrize(("method", "path"), [("post", "/api/extension/auth/codes"), ("get", "/api/extension/sessions"), ("delete", f"/api/extension/sessions/{uuid.uuid4()}")])
def test_an_extension_token_can_not_use_the_website_routes(world: World, method: str, path: str) -> None:
    tokens = world.login()
    kwargs: dict[str, Any] = {"headers": {"Authorization": f"Bearer {tokens['access_token']}"}}
    if method == "post":
        kwargs["json"] = world.code_body(pkce_pair()[1])
    assert_error(getattr(world.client, method)(path, **kwargs), 401, "not_authenticated")


@pytest.mark.parametrize("header", [None, "Bearer", "Bearer ", "Basic abc", "Bearer not.a.jwt"])
def test_a_missing_or_malformed_token_is_not_authenticated(world: World, header: str | None) -> None:
    headers = {**CLIENT, **({"Authorization": header} if header is not None else {})}
    assert_error(world.client.get("/api/extension/me", headers=headers), 401, "not_authenticated")


def test_an_expired_access_token_is_token_expired(world: World) -> None:
    from datetime import datetime, timedelta, timezone

    from jose import jwt

    tokens = world.login()
    expired = jwt.encode(
        {
            "sub": str(world.user),
            "type": "access",
            "scope": "extension",
            "sid": tokens["session_id"],
            "exp": datetime.now(timezone.utc) - timedelta(seconds=1),
        },
        world.settings.jwt_secret_key,
        algorithm="HS256",
    )
    assert_error(world.client.get("/api/extension/me", headers={"Authorization": f"Bearer {expired}", **CLIENT}), 401, "token_expired")


def test_disconnecting_on_the_website_signs_the_device_out_at_once(world: World) -> None:
    tokens = world.login()
    assert world.client.get("/api/extension/me", headers=world.headers(tokens)).status_code == 200
    deleted = world.client.delete(f"/api/extension/sessions/{tokens['session_id']}", headers=world.web_headers())
    assert deleted.status_code == 204
    assert_error(world.client.get("/api/extension/me", headers=world.headers(tokens)), 401, "session_revoked")
    assert_error(world.client.delete(f"/api/extension/sessions/{tokens['session_id']}", headers=world.web_headers()), 404, "not_found")


@pytest.mark.parametrize("session_id", ["not-a-uuid", "123"])
def test_disconnecting_a_malformed_id_is_404(world: World, session_id: str) -> None:
    assert_error(world.client.delete(f"/api/extension/sessions/{session_id}", headers=world.web_headers()), 404, "not_found")


def test_one_user_can_not_see_or_end_anothers_sessions(world: World) -> None:
    tokens = world.login()
    other = world.store.add_user()
    assert world.client.get("/api/extension/sessions", headers=world.web_headers(other)).json() == {"sessions": []}
    assert_error(world.client.delete(f"/api/extension/sessions/{tokens['session_id']}", headers=world.web_headers(other)), 404, "not_found")
    assert world.client.get("/api/extension/me", headers=world.headers(tokens)).status_code == 200


def test_the_device_list_has_the_contract_shape(world: World) -> None:
    tokens = world.login()
    listed = world.client.get("/api/extension/sessions", headers=world.web_headers()).json()["sessions"]
    assert listed == [
        {
            "id": tokens["session_id"],
            "device_name": "VANSH-LAPTOP (win32)",
            "app_name": "Visual Studio Code",
            "ext_version": "0.1.0",
            "created_at": listed[0]["created_at"],
            "last_used_at": listed[0]["last_used_at"],
            "expires_at": listed[0]["expires_at"],
            "current": False,
        }
    ]


# ---- the switch and the version gate -----------------------------------------------------------

EVERY_ROUTE = [
    ("post", "/api/extension/auth/codes"),
    ("post", "/api/extension/auth/token"),
    ("post", "/api/extension/auth/refresh"),
    ("post", "/api/extension/auth/logout"),
    ("get", "/api/extension/me"),
    ("post", "/api/extension/chat"),
    ("post", "/api/extension/reports"),
    ("get", "/api/extension/sessions"),
    ("delete", f"/api/extension/sessions/{uuid.uuid4()}"),
]


@pytest.mark.parametrize(("method", "path"), EVERY_ROUTE)
def test_switched_off_every_route_is_503_and_the_website_is_unaffected(method: str, path: str) -> None:
    world = World(extension_enabled=False)
    response = getattr(world.client, method)(path, headers=world.web_headers())
    body = assert_error(response, 503, "service_unavailable")
    assert body["retry_after"] == 60 and "coming soon" in body["detail"]
    assert world.client.get("/api/auth/me", headers=world.web_headers()).status_code == 200
    assert world.client.get("/api/assistant/usage", headers=world.web_headers()).status_code == 200


@pytest.mark.parametrize("path", ["/api/extension/me", "/api/extension/chat"])
def test_an_outdated_extension_gets_426(path: str) -> None:
    world = World(extension_min_version="0.1.0")
    tokens = world.login()
    headers = {"Authorization": f"Bearer {tokens['access_token']}", "X-WebNest-Client": "vscode/0.0.9 (Visual Studio Code)"}
    response = world.client.get(path, headers=headers) if path.endswith("/me") else world.client.post(path, json=chat_body(), headers=headers)
    assert_error(response, 426, "extension_outdated")
    assert world.provider.requests == []


@pytest.mark.parametrize("client", ["vscode/0.1.0 (Visual Studio Code)", "vscode/0.10.0", "vscode/1.0.0", "something odd", ""])
def test_a_current_or_unnamed_client_passes_the_gate(world: World, client: str) -> None:
    tokens = world.login()
    response = world.client.get("/api/extension/me", headers={"Authorization": f"Bearer {tokens['access_token']}", "X-WebNest-Client": client})
    assert response.status_code == 200


def test_the_gate_is_checked_before_the_token(world: World) -> None:
    world.settings.extension_min_version = "0.2.0"
    assert_error(world.client.get("/api/extension/me", headers=CLIENT), 426, "extension_outdated")


# ---- 2.6 /me -------------------------------------------------------------------------------------


def test_me_has_the_contract_shape(world: World) -> None:
    world.give_prime(used=123_456)
    tokens = world.login()
    body = world.client.get("/api/extension/me", headers=world.headers(tokens)).json()
    assert set(body) == {"user", "access", "service", "limits", "links", "extension"}
    assert body["user"] == {"id": str(world.user), "email": "asha@example.com", "full_name": "Asha Verma"}
    assert set(body["access"]) == {
        "active", "is_prime", "member_label", "expires_at", "tokens_limit", "tokens_used", "daily_tokens_limit",
        "daily_tokens_used", "weekly_tokens_limit", "weekly_tokens_used", "resets_at", "price_paise", "price_display",
        "pass_days", "cap",
    }
    assert body["access"]["price_display"] == "₹299" and body["access"]["tokens_used"] == 123_456
    assert body["access"]["expires_at"].endswith("Z")
    assert body["limits"]["max_input_tokens"] == 12000 and body["limits"]["max_context_chars"] == 40000
    assert body["extension"] == {"min_version": "0.1.0", "latest_version": "0.1.0"}
    assert body["service"] == {"enabled": True, "provider_name": "WebNest AI", "message": None}


def test_a_payment_on_the_website_shows_on_the_next_me_without_logging_in_again(world: World) -> None:
    tokens = world.login()
    before = world.client.get("/api/extension/me", headers=world.headers(tokens)).json()["access"]
    assert (before["active"], before["is_prime"], before["cap"]) == (False, False, None)
    assert_error(world.client.post("/api/extension/chat", json=chat_body(), headers=world.headers(tokens)), 402, "prime_required")
    world.give_prime()
    after = world.client.get("/api/extension/me", headers=world.headers(tokens)).json()["access"]
    assert (after["active"], after["is_prime"]) == (True, True)
    assert world.client.post("/api/extension/chat", json=chat_body(), headers=world.headers(tokens)).status_code == 200


def test_the_daily_cap_from_another_app_shows_in_vscode(world: World) -> None:
    world.give_prime()
    world.usage.today_total = 150_000
    world.usage.week_total = 150_000
    tokens = world.login()
    access = world.client.get("/api/extension/me", headers=world.headers(tokens)).json()["access"]
    assert (access["active"], access["cap"]) == (False, "day") and access["resets_at"].endswith("T00:00:00Z")


def test_a_database_failure_on_me_is_a_coded_503_without_details(world: World) -> None:
    tokens = world.login()
    world.usage.fail_totals = True
    body = assert_error(world.client.get("/api/extension/me", headers=world.headers(tokens)), 503, "service_unavailable")
    assert "assistant usage" not in body["detail"] and body["retry_after"] == 5


def test_me_is_rate_limited_per_user(world: World) -> None:
    tokens = world.login()
    world.limits = type(world.limits)({"me": 2})
    for _ in range(2):
        assert world.client.get("/api/extension/me", headers=world.headers(tokens)).status_code == 200
    assert_error(world.client.get("/api/extension/me", headers=world.headers(tokens)), 429, "rate_limited")


# ---- 2.7 chat: the Prime gate (must pass before launch) ---------------------------------------


def test_without_prime_chat_is_402_and_the_provider_is_never_called(world: World) -> None:
    tokens = world.login()
    body = assert_error(world.client.post("/api/extension/chat", json=chat_body(), headers=world.headers(tokens)), 402, "prime_required")
    assert "Prime" in body["detail"]
    assert world.provider.requests == []
    assert world.usage.reservations == []


def test_a_used_up_pass_is_402_pass_allowance_used_and_the_provider_is_never_called(world: World) -> None:
    world.give_prime(used=2_000_000)
    tokens = world.login()
    assert_error(world.client.post("/api/extension/chat", json=chat_body(), headers=world.headers(tokens)), 402, "pass_allowance_used")
    assert world.provider.requests == []


def test_a_pass_that_expired_during_the_login_is_prime_required_and_me_agrees(world: World) -> None:
    world.give_prime()
    tokens = world.login()
    assert world.client.post("/api/extension/chat", json=chat_body(), headers=world.headers(tokens)).status_code == 200
    world.give_prime(days=-1)
    calls = len(world.provider.requests)
    assert_error(world.client.post("/api/extension/chat", json=chat_body(), headers=world.headers(tokens)), 402, "prime_required")
    assert len(world.provider.requests) == calls
    assert world.client.get("/api/extension/me", headers=world.headers(tokens)).json()["access"]["is_prime"] is False


def test_a_direct_api_call_without_the_extension_is_gated_the_same(world: World) -> None:
    tokens = world.login()
    response = world.client.post("/api/extension/chat", json=chat_body(), headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert_error(response, 402, "prime_required")
    assert world.provider.requests == []


# ---- 2.7 chat: limits and refusals ----------------------------------------------------------------


@pytest.mark.parametrize(("today", "week", "code"), [(150_000, 150_000, "daily_allowance_used"), (0, 700_000, "weekly_allowance_used")])
def test_the_daily_and_weekly_caps_are_coded_429s(world: World, today: int, week: int, code: str) -> None:
    world.give_prime()
    world.usage.user_tokens = today
    world.usage.week_tokens = week
    tokens = world.login()
    body = assert_error(world.client.post("/api/extension/chat", json=chat_body(), headers=world.headers(tokens)), 429, code)
    assert 0 < body["retry_after"] <= 86_401
    assert world.provider.requests == []
    # Refused before any reply: the pass got its reservation back.
    assert world.passes.access[world.user].tokens_used == 0


def test_the_free_daily_calls_are_rate_limited_with_the_paywall_off() -> None:
    world = World(paywall=False)
    world.usage.calls = 40
    tokens = world.login()
    assert_error(world.client.post("/api/extension/chat", json=chat_body(), headers=world.headers(tokens)), 429, "rate_limited")


def test_with_the_paywall_off_a_free_user_can_chat() -> None:
    world = World(paywall=False)
    tokens = world.login()
    assert world.client.get("/api/extension/me", headers=world.headers(tokens)).json()["access"]["active"] is True
    assert world.client.post("/api/extension/chat", json=chat_body(), headers=world.headers(tokens)).status_code == 200


def test_one_stream_per_user_across_the_website_and_vscode(world: World) -> None:
    world.give_prime()
    tokens = world.login()
    assert world.registry.acquire(world.user)  # a reply streaming on Android or the website
    assert_error(world.client.post("/api/extension/chat", json=chat_body(), headers=world.headers(tokens)), 429, "stream_in_progress")
    world.registry.release(world.user)
    assert world.client.post("/api/extension/chat", json=chat_body(), headers=world.headers(tokens)).status_code == 200


def test_a_vscode_reply_in_flight_blocks_the_website_too(world: World) -> None:
    async def scenario() -> None:
        world.give_prime()
        hanging = FakeProvider([StreamOpened(), TextDelta("thinking")], hang=True)
        world.assistant._provider = hanging
        from schemas.extension_schemas import ExtensionChatRequest

        stream = await world.chat_service.open_chat(world.user, ExtensionChatRequest.model_validate(chat_body()))
        try:
            from schemas.assistant_schemas import ChatContext, ChatMessage, ChatRequest

            from services.assistant_service import AssistantRateLimitedError

            website = ChatRequest(mode="ask", messages=[ChatMessage(role="user", content="hi")], context=ChatContext(language="python"))
            with pytest.raises(AssistantRateLimitedError) as caught:
                await world.assistant.open_chat(world.user, website)
            assert caught.value.code == "stream_in_progress"
        finally:
            await stream.close()
        assert hanging.closed

    asyncio.run(scenario())


def test_a_busy_provider_is_503_provider_busy(world: World) -> None:
    world.give_prime()
    world.assistant._provider = FakeProvider([ProviderRateLimitedError(retry_after=12)])
    tokens = world.login()
    body = assert_error(world.client.post("/api/extension/chat", json=chat_body(), headers=world.headers(tokens)), 503, "provider_busy")
    assert body["retry_after"] == 12
    assert world.passes.access[world.user].tokens_used == 0


@pytest.mark.parametrize("changes", [{"assistant_enabled": False}, {"assistant_groq_api_key": ""}])
def test_the_assistant_off_is_503_service_unavailable(changes: dict[str, Any]) -> None:
    world = World(**changes)
    world.give_prime()
    tokens = world.login()
    body = assert_error(world.client.post("/api/extension/chat", json=chat_body(), headers=world.headers(tokens)), 503, "service_unavailable")
    assert body["retry_after"] == 60
    assert world.client.get("/api/extension/me", headers=world.headers(tokens)).json()["service"]["enabled"] is False


def test_a_provider_that_is_down_is_503_service_unavailable(world: World) -> None:
    world.give_prime()
    world.assistant._provider = FakeProvider([ProviderUnavailableError()])
    tokens = world.login()
    assert_error(world.client.post("/api/extension/chat", json=chat_body(), headers=world.headers(tokens)), 503, "service_unavailable")


def test_a_failure_mid_reply_is_an_error_event_after_start_and_charges_only_what_was_delivered(world: World) -> None:
    world.give_prime()
    world.assistant._provider = FakeProvider([StreamOpened(), ProviderStreamError("provider_error")])
    tokens = world.login()
    response = world.client.post("/api/extension/chat", json=chat_body(), headers=world.headers(tokens))
    events = parse_sse(response.text)
    assert [name for name, _ in events] == ["start", "error"]
    assert events[1][1]["code"] == "provider_error"
    assert world.passes.access[world.user].tokens_used == 0


def test_a_reply_cut_at_the_length_limit_says_length(world: World) -> None:
    world.give_prime()
    world.assistant._provider = FakeProvider([StreamOpened(), TextDelta("part"), UsageEvent(10, 2000), FinishedEvent("length")])
    tokens = world.login()
    events = parse_sse(world.client.post("/api/extension/chat", json=chat_body(), headers=world.headers(tokens)).text)
    assert events[-1] == ("done", {"finish_reason": "length"})


def test_a_body_over_512_kb_is_413_payload_too_large(world: World) -> None:
    world.give_prime()
    tokens = world.login()
    huge = chat_body(context={"active_file": {"path": "a.py", "content": "x" * 600_000}})
    assert_error(world.client.post("/api/extension/chat", json=huge, headers=world.headers(tokens)), 413, "payload_too_large")
    assert world.provider.requests == []


def test_a_conversation_too_large_for_the_model_is_413_conversation_too_large() -> None:
    world = World(extension_max_input_tokens=400_000, extension_max_context_chars=400_000)
    world.give_prime()
    tokens = world.login()
    body = chat_body(context={"active_file": {"path": "a.py", "content": "y" * 59_000}})
    body["messages"] = [{"role": "user", "content": "q" * 3999}, {"role": "assistant", "content": "a" * 59_000}] * 4 + [{"role": "user", "content": "go"}]
    assert_error(world.client.post("/api/extension/chat", json=body, headers=world.headers(tokens)), 413, "conversation_too_large")
    assert world.provider.requests == []


def test_a_bad_chat_body_is_422_and_never_echoes_the_code(world: World) -> None:
    world.give_prime()
    tokens = world.login()
    secret = "API_KEY_SUPER_SECRET_123"
    bad = chat_body(context={"active_file": {"path": "a.py", "content": secret, "segments": [{"start_line": 1, "end_line": 1, "text": secret}] * 9}})
    response = world.client.post("/api/extension/chat", json=bad, headers=world.headers(tokens))
    body = assert_error(response, 422, "validation_error")
    assert secret not in response.text
    assert body["errors"][0]["loc"] == ["body", "context", "active_file", "segments"]


def test_segments_over_the_budget_are_422(world: World) -> None:
    world.give_prime()
    tokens = world.login()
    segments = [{"start_line": n * 100 + 1, "end_line": n * 100 + 2, "text": "z" * 10_001} for n in range(4)]
    response = world.client.post(
        "/api/extension/chat", json=chat_body(context={"active_file": {"path": "a.py", "segments": segments}}), headers=world.headers(tokens)
    )
    assert_error(response, 422, "validation_error")


def test_an_attachment_is_422(world: World) -> None:
    world.give_prime()
    tokens = world.login()
    response = world.client.post(
        "/api/extension/chat", json=chat_body(context={"attachments": [{"kind": "image", "data": "..."}]}), headers=world.headers(tokens)
    )
    assert_error(response, 422, "validation_error")


# ---- 2.7 chat: what the model gets ----------------------------------------------------------------


def test_the_fix_example_from_the_contract(world: World) -> None:
    world.give_prime()
    tokens = world.login()
    context = {
        "workspace_name": "demo-workspace",
        "os": "win32",
        "shell": "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
        "ui_language": "en",
        "files": ["src/cart.ts", "src/report.py"],
        "files_truncated": False,
        "active_file": {
            "path": "src/cart.ts",
            "language": "typescript",
            "content": "interface Item {\n  name: string;\n  price: number;\n}\n",
            "truncated": False,
            "selection": {"start_line": 6, "end_line": 12, "text": "export function total(items: Item[]): number {"},
        },
        "diagnostics": [{"path": "src/cart.ts", "line": 9, "severity": "error", "message": "Property 'cost' does not exist on type 'Item'.", "source": "ts"}],
        "attachments": [],
    }
    response = world.client.post("/api/extension/chat", json=chat_body("Fix the problem at lines 6-12", context), headers=world.headers(tokens))
    assert response.status_code == 200
    sent = sent_user_text(world.provider)
    assert "VS Code file: src/cart.ts (typescript)" in sent and "--- selected lines 6-12 ---" in sent
    assert "src/cart.ts:9 error [ts]: Property 'cost' does not exist on type 'Item'." in sent
    assert sent.rstrip().endswith("Fix the problem at lines 6-12")
    request = world.provider.requests[-1]
    assert request.tools == [] and "inside Visual Studio Code" in request.system


def test_an_empty_context_still_answers_over_http(world: World) -> None:
    world.give_prime()
    tokens = world.login()
    for context in ({}, None):
        body = chat_body(context=context or {})
        if context is None:
            body.pop("context")
        assert world.client.post("/api/extension/chat", json=body, headers=world.headers(tokens)).status_code == 200


def test_a_withheld_file_sends_the_note_and_none_of_the_file(world: World) -> None:
    world.give_prime()
    tokens = world.login()
    context = {"active_file": {"path": ".env", "content": "DB_PASSWORD=hunter2", "withheld": "secret"}}
    assert world.client.post("/api/extension/chat", json=chat_body("why", context), headers=world.headers(tokens)).status_code == 200
    sent = json.dumps(world.provider.requests[-1].messages)
    assert "hunter2" not in sent and "was not shared because it seems to contain a secret" in sent


def test_extension_0_1_0_sends_60000_characters_and_is_cut_to_the_budget(world: World) -> None:
    world.give_prime()
    tokens = world.login()
    content = "".join(f"line {n:06d}\n" for n in range(6000))[:60_000]
    context = {"active_file": {"path": "big.py", "content": content, "selection": {"start_line": 3, "end_line": 4, "text": "SELECTED LINES"}}}
    response = world.client.post("/api/extension/chat", json=chat_body("explain", context), headers=world.headers(tokens))
    assert response.status_code == 200
    sent = sent_user_text(world.provider)
    assert "SELECTED LINES" in sent and "characters cut" in sent and len(sent) < 41_500
    assert world.usage.call_log[-1].sizes.file_chars <= 40_000
    # Cut once, by the adapter, never again by the optimizer.
    assert sent.count("characters cut") == 1 and "was not shared to save space" not in sent


def test_the_public_assistant_route_refuses_the_vscode_language(world: World) -> None:
    body = {"mode": "ask", "messages": [{"role": "user", "content": "hi"}], "context": {"language": "vscode"}}
    response = world.client.post("/api/assistant/chat", json=body, headers=world.web_headers())
    assert response.status_code == 422
    assert world.provider.requests == []


# ---- 2.8 reports ------------------------------------------------------------------------------


def test_a_report_with_the_extension_token(world: World) -> None:
    tokens = world.login()
    body = {"reason": "misleading", "details": "Uses a field that doesn't exist.", "reply_text": "`Item` has no `cost`", "prompt_text": "Fix", "client_message_id": "a1"}
    response = world.client.post("/api/extension/reports", json=body, headers=world.headers(tokens))
    assert response.status_code == 201 and response.json()["status"] == "open"
    assert world.reports.reports[0][0] == world.user


@pytest.mark.parametrize("body", [{"reason": "spam", "reply_text": "x"}, {"reason": "other", "reply_text": "  "}, {}])
def test_a_bad_report_is_422(world: World, body: dict[str, Any]) -> None:
    tokens = world.login()
    assert_error(world.client.post("/api/extension/reports", json=body, headers=world.headers(tokens)), 422, "validation_error")


# ---- unexpected failures stay coded --------------------------------------------------------------


def test_an_unknown_route_or_method_is_coded(world: World) -> None:
    assert_error(world.client.get("/api/extension/nothing-here"), 404, "not_found")
    assert_error(world.client.put("/api/extension/me"), 405, "method_not_allowed")


def test_a_database_outage_during_authentication_is_a_coded_503(world: World) -> None:
    tokens = world.login()
    world.store.fail = True
    body = assert_error(world.client.get("/api/extension/me", headers=world.headers(tokens)), 503, "service_unavailable")
    assert "session" not in body["detail"].lower() or "records" in body["detail"]


def test_an_unexpected_exception_is_a_coded_500_without_its_text(world: World) -> None:
    class Broken:
        async def me(self, session_user: Any) -> Any:
            raise RuntimeError("internal detail: password=hunter2")

    from core.dependencies import get_extension_chat_service

    world.app.dependency_overrides[get_extension_chat_service] = lambda: Broken()
    tokens = world.login()
    response = world.client.get("/api/extension/me", headers=world.headers(tokens))
    assert_error(response, 500, "internal_error")
    assert "hunter2" not in response.text


def test_other_routes_keep_their_own_error_shape(world: World) -> None:
    response = world.client.get("/api/assistant/usage")
    assert response.status_code == 401 and response.json() == {"detail": "Missing bearer token"}
    assert "x-request-id" not in response.headers


def test_the_streams_are_closed_and_the_slot_freed_after_each_reply(world: World) -> None:
    world.give_prime()
    tokens = world.login()
    for _ in range(3):
        assert world.client.post("/api/extension/chat", json=chat_body(), headers=world.headers(tokens)).status_code == 200
    assert world.registry.acquire(world.user)
    assert len(world.usage.recorded) == 3 and world.provider.closed


def test_text_events_default_is_text_only() -> None:
    assert all(type(event).__name__ != "ToolCallEvent" for event in TEXT_EVENTS)


def test_a_database_error_from_the_store_never_leaks_through_codes(world: World) -> None:
    world.store.fail = True
    _, challenge = pkce_pair()
    body = assert_error(
        world.client.post("/api/extension/auth/codes", json=world.code_body(challenge), headers=world.web_headers()), 503, "service_unavailable"
    )
    assert isinstance(body["retry_after"], int)
    assert DatabaseError.__name__ not in json.dumps(body)
