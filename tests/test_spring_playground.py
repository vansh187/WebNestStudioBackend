"""Spring Boot playground proxy: input checks, login, limits, the daily cap, and how each answer
from the Cloud Run playground is passed on. No network and no database."""

import asyncio
import functools
import json
import os
import uuid
from types import SimpleNamespace

os.environ.setdefault("SUPABASE_URL", "postgresql://user:pass@localhost:5432/test")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import app
from core.dependencies import get_current_user, get_spring_playground_service
from core.exceptions import BadRequestError, ExternalServiceError, PayloadTooLargeError, RateLimitedError
from schemas.spring_playground_schemas import SpringRunRequest
from services.coding_service import SlidingWindowLimiter
from services.spring_playground_service import SpringPlaygroundService

MAIN = "com.example.demo.DemoApplication"
MAIN_FILE = {"path": "src/main/java/com/example/demo/DemoApplication.java", "content": "package com.example.demo; class DemoApplication {}"}
URL = "https://spring.example.run.app"
USER = SimpleNamespace(id=uuid.uuid4())


def runs(test):
    """Runs an async test with asyncio.run, as the rest of this suite does (no pytest plugin)."""

    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))

    return wrapper


def body(**change) -> dict:
    return {"files": [MAIN_FILE], "mainClass": MAIN, "requests": [{"method": "get", "path": "/api/tasks"}], **change}


def settings(**change) -> SimpleNamespace:
    values = {
        "spring_playground_url": URL,
        "spring_playground_invoker_key": '{"type": "service_account"}',
        "java_playground_invoker_key": "",
        "spring_playground_timeout_seconds": 5.0,
        "spring_playground_daily_cap": 500,
        "spring_playground_per_minute": 4,
        "spring_playground_per_day": 60,
    }
    values.update(change)
    return SimpleNamespace(**values)


class FakeUsage:
    def __init__(self, start: int = 0, fail: bool = False) -> None:
        self.runs = start
        self.fail = fail

    async def increment(self, day) -> int:
        if self.fail:
            raise RuntimeError("database down")
        self.runs += 1
        return self.runs

    async def decrement(self, day) -> None:
        self.runs = max(self.runs - 1, 0)


class FakeTokens:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.resets = 0

    async def get_token(self, key_json: str, audience: str) -> str:
        self.calls.append((key_json, audience))
        return "google-id-token"

    def reset(self) -> None:
        self.resets += 1


@pytest.fixture(autouse=True)
def fresh_limits(monkeypatch):
    monkeypatch.setattr(SpringPlaygroundService, "_minute_limiter", SlidingWindowLimiter())
    monkeypatch.setattr(SpringPlaygroundService, "_day_limiter", SlidingWindowLimiter())
    tokens = FakeTokens()
    monkeypatch.setattr(SpringPlaygroundService, "_token_provider", tokens)
    return tokens


def make_service(handler, usage: FakeUsage | None = None, **settings_change) -> tuple[SpringPlaygroundService, list[httpx.Request]]:
    sent: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return handler(request)

    service = SpringPlaygroundService(session=None, settings=settings(**settings_change), transport=httpx.MockTransport(record))
    service._usage = usage or FakeUsage()
    return service, sent


def ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"requestId": "r1", "status": "success", "phase": "requests", "responses": []})


# ---- Input -------------------------------------------------------------------------------------


def test_a_valid_project_becomes_the_playground_body() -> None:
    parsed = SpringRunRequest.model_validate(body(requests=[{"method": "post", "path": "/api/tasks", "headers": {"Content-Type": "application/json"}, "body": "{}"}]))
    assert parsed.to_playground_body() == {
        "files": [MAIN_FILE],
        "mainClass": MAIN,
        "requests": [{"method": "POST", "path": "/api/tasks", "headers": {"Content-Type": "application/json"}, "body": "{}"}],
    }


@pytest.mark.parametrize(
    "change",
    [
        {"files": []},
        {"files": [MAIN_FILE, {"path": "../evil.java", "content": ""}]},
        {"files": [MAIN_FILE, {"path": "src/main/resources/application.yml", "content": ""}]},
        {"files": [MAIN_FILE, {"path": "pom.xml", "content": ""}]},
        {"files": [MAIN_FILE, MAIN_FILE]},
        {"files": [MAIN_FILE, {"path": "src/main/java/com/example/demo/Big.java", "content": "a" * 262144}]},
        {"files": [{"path": f"src/main/java/p/C{i}.java", "content": ""} for i in range(61)]},
        {"mainClass": "DemoApplication"},
        {"mainClass": "com.example.demo.Missing"},
        {"requests": [{"method": "TRACE", "path": "/"}]},
        {"requests": [{"path": "http://evil.example/"}]},
        {"requests": [{"path": "//evil.example/"}]},
        {"requests": [{"path": "/a b"}]},
        {"requests": [{"path": "/", "headers": {"X-A": "one\r\nX-B: two"}}]},
        {"requests": [{"path": "/", "body": "a" * 65537}]},
        {"requests": [{"path": "/"}] * 31},
    ],
)
def test_bad_projects_are_rejected_before_a_run(change: dict) -> None:
    with pytest.raises(ValidationError):
        SpringRunRequest.model_validate(body(**change))


def test_a_project_and_requests_too_large_together_are_rejected() -> None:
    big_file = {"path": "src/main/java/com/example/demo/Big.java", "content": "a" * 200_000}
    requests = [{"method": "POST", "path": "/api/tasks", "body": "b" * 60_000} for _ in range(6)]
    with pytest.raises(ValidationError, match="too large together"):
        SpringRunRequest.model_validate(body(files=[MAIN_FILE, big_file], requests=requests))
    # Each part alone is fine.
    SpringRunRequest.model_validate(body(files=[MAIN_FILE, big_file]))
    SpringRunRequest.model_validate(body(requests=requests))


# ---- Forwarding ------------------------------------------------------------------------------------


@runs
async def test_a_run_is_forwarded_with_our_token_and_passed_through(fresh_limits) -> None:
    service, sent = make_service(ok)
    result = await service.run(SpringRunRequest.model_validate(body()), USER)
    assert json.loads(result)["status"] == "success"
    assert len(sent) == 1
    assert str(sent[0].url) == f"{URL}/api/project/run"
    assert sent[0].headers["Authorization"] == "Bearer google-id-token"
    assert json.loads(sent[0].content)["mainClass"] == MAIN
    assert fresh_limits.calls == [('{"type": "service_account"}', URL)]


@runs
async def test_the_java_invoker_key_is_used_when_there_is_no_spring_one(fresh_limits) -> None:
    service, _ = make_service(ok, spring_playground_invoker_key="", java_playground_invoker_key="java-key")
    await service.run(SpringRunRequest.model_validate(body()), USER)
    assert fresh_limits.calls[0][0] == "java-key"


@pytest.mark.parametrize("url, key", [("", "key"), ("http://insecure.example", "key"), (URL, "")])
@runs
async def test_an_unconfigured_runner_says_so_and_sends_nothing(url: str, key: str) -> None:
    service, sent = make_service(ok, spring_playground_url=url, spring_playground_invoker_key=key)
    with pytest.raises(ExternalServiceError, match="not configured"):
        await service.run(SpringRunRequest.model_validate(body()), USER)
    assert sent == []


@pytest.mark.parametrize(
    "status, payload, error, text",
    [
        (429, {"error": "busy"}, RateLimitedError, "busy"),
        (400, {"error": "mainClass must match a file"}, BadRequestError, "mainClass must match a file"),
        (413, {"error": "Request exceeds 512 KiB"}, PayloadTooLargeError, "512 KiB"),
        (500, {"error": "boom"}, ExternalServiceError, "unavailable"),
        (404, {"error": "Not found"}, ExternalServiceError, "unavailable"),
    ],
)
@runs
async def test_playground_errors_become_clean_errors(status: int, payload: dict, error: type, text: str) -> None:
    service, _ = make_service(lambda _r: httpx.Response(status, json=payload))
    with pytest.raises(error, match=text):
        await service.run(SpringRunRequest.model_validate(body()), USER)


@runs
async def test_a_refused_token_is_dropped_so_the_next_run_mints_a_new_one(fresh_limits) -> None:
    service, _ = make_service(lambda _r: httpx.Response(403, json={}))
    with pytest.raises(ExternalServiceError):
        await service.run(SpringRunRequest.model_validate(body()), USER)
    assert fresh_limits.resets == 1


@pytest.mark.parametrize(
    "handler",
    [
        lambda _r: httpx.Response(200, content=b"not json"),
        lambda _r: httpx.Response(200, json=["no", "status"]),
    ],
)
@runs
async def test_a_strange_answer_is_unavailable_not_a_crash(handler) -> None:
    service, _ = make_service(handler)
    with pytest.raises(ExternalServiceError, match="unavailable"):
        await service.run(SpringRunRequest.model_validate(body()), USER)


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("slow"), httpx.ConnectError("down")])
@runs
async def test_timeouts_and_network_errors_are_unavailable(failure: Exception) -> None:
    def handler(_r):
        raise failure

    service, _ = make_service(handler)
    with pytest.raises(ExternalServiceError, match="unavailable"):
        await service.run(SpringRunRequest.model_validate(body()), USER)


# ---- Limits ------------------------------------------------------------------------------------------


@runs
async def test_each_user_has_a_per_minute_limit() -> None:
    service, sent = make_service(ok)
    project = SpringRunRequest.model_validate(body())
    for _ in range(4):
        await service.run(project, USER)
    with pytest.raises(RateLimitedError, match="in a minute"):
        await service.run(project, USER)
    # Another user is not affected.
    await service.run(project, SimpleNamespace(id=uuid.uuid4()))
    assert len(sent) == 4 + 1


@runs
async def test_per_user_limits_come_from_the_settings() -> None:
    service, sent = make_service(ok, spring_playground_per_minute=10, spring_playground_per_day=2)
    project = SpringRunRequest.model_validate(body())
    await service.run(project, USER)
    await service.run(project, USER)
    with pytest.raises(RateLimitedError, match="today's Spring Boot run limit"):
        await service.run(project, USER)
    assert len(sent) == 2


@runs
async def test_a_busy_playground_does_not_use_up_any_limit() -> None:
    usage = FakeUsage()
    service, sent = make_service(lambda _r: httpx.Response(429, json={"error": "busy"}), usage=usage, spring_playground_per_minute=2)
    project = SpringRunRequest.model_validate(body())
    for _ in range(5):
        with pytest.raises(RateLimitedError, match="busy"):
            await service.run(project, USER)
    # Every try reached the playground: none of them used up the 2-per-minute limit or the cap.
    assert len(sent) == 5
    assert usage.runs == 0


@runs
async def test_the_site_wide_daily_cap_pauses_runs() -> None:
    service, sent = make_service(ok, usage=FakeUsage(start=500))
    with pytest.raises(RateLimitedError, match="paused for today"):
        await service.run(SpringRunRequest.model_validate(body()), USER)
    assert sent == []


@runs
async def test_without_the_counter_runs_fail_closed() -> None:
    service, sent = make_service(ok, usage=FakeUsage(fail=True))
    with pytest.raises(ExternalServiceError, match="unavailable"):
        await service.run(SpringRunRequest.model_validate(body()), USER)
    assert sent == []


# ---- Route ---------------------------------------------------------------------------------------------


class FakeService:
    def __init__(self) -> None:
        self.calls = []

    async def run(self, payload, user):
        self.calls.append((payload, user))
        return b'{"status":"success","responses":[]}'


@pytest.fixture
def client():
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_the_route_needs_a_login(client) -> None:
    fake = FakeService()
    app.dependency_overrides[get_spring_playground_service] = lambda: fake
    response = client.post("/api/spring/run", json=body())
    assert response.status_code == 401
    assert fake.calls == []


def test_the_route_passes_the_playground_answer_through(client) -> None:
    fake = FakeService()
    app.dependency_overrides[get_spring_playground_service] = lambda: fake
    app.dependency_overrides[get_current_user] = lambda: USER
    response = client.post("/api/spring/run", json=body(), headers={"Authorization": "Bearer x"})
    assert response.status_code == 200
    assert response.json() == {"status": "success", "responses": []}
    assert fake.calls[0][1] is USER


def test_the_route_rejects_bad_input_with_422(client) -> None:
    fake = FakeService()
    app.dependency_overrides[get_spring_playground_service] = lambda: fake
    app.dependency_overrides[get_current_user] = lambda: USER
    response = client.post("/api/spring/run", json=body(mainClass="Nope"), headers={"Authorization": "Bearer x"})
    assert response.status_code == 422
    assert fake.calls == []
