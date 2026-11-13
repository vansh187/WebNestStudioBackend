"""Node.js playground proxy: input checks, login, limits, the daily cap, the package list, and how
each answer from the Cloud Run playground is passed on. No network and no database."""

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
from core.dependencies import get_current_user, get_node_playground_service
from core.exceptions import BadRequestError, ExternalServiceError, PayloadTooLargeError, RateLimitedError
from schemas.node_playground_schemas import NodeProjectRunRequest, NodeRunRequest, is_node_path
from services.coding_service import SlidingWindowLimiter
from services.node_playground_service import NodePlaygroundService

URL = "https://node.example.run.app"
USER = SimpleNamespace(id=uuid.uuid4())
MAIN = {"path": "main.js", "content": "console.log('hi')"}
SERVER = {"path": "server.js", "content": "require('express')().listen(3000)"}


def runs(test):
    """Runs an async test with asyncio.run, as the rest of this suite does (no pytest plugin)."""

    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))

    return wrapper


def script(**change) -> dict:
    return {"files": [MAIN], "stdin": "Asha\n", **change}


def project(**change) -> dict:
    return {"files": [SERVER], "requests": [{"method": "get", "path": "/api/tasks"}], **change}


def settings(**change) -> SimpleNamespace:
    values = {
        "node_playground_url": URL,
        "node_playground_invoker_key": '{"type": "service_account"}',
        "spring_playground_url": "",
        "spring_playground_invoker_key": "",
        "java_playground_invoker_key": "",
        "node_playground_timeout_seconds": 5.0,
        "node_playground_daily_cap": 3000,
        "node_playground_per_minute": 10,
        "node_playground_per_day": 200,
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
    monkeypatch.setattr(NodePlaygroundService, "_minute_limiter", SlidingWindowLimiter())
    monkeypatch.setattr(NodePlaygroundService, "_day_limiter", SlidingWindowLimiter())
    monkeypatch.setattr(NodePlaygroundService, "_packages_cache", None)
    monkeypatch.setattr(NodePlaygroundService, "_packages_lock", None)
    tokens = FakeTokens()
    monkeypatch.setattr(NodePlaygroundService, "_token_provider", tokens)
    return tokens


def make_service(handler, usage: FakeUsage | None = None, **settings_change) -> tuple[NodePlaygroundService, list[httpx.Request]]:
    sent: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return handler(request)

    service = NodePlaygroundService(session=None, settings=settings(**settings_change), transport=httpx.MockTransport(record))
    service._usage = usage or FakeUsage()
    return service, sent


def ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"requestId": "r1", "status": "success", "phase": "run", "run": {"stdout": "hi\n"}})


# ---- Input -------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["main.js", "src/app.mjs", "lib/util.cjs", "package.json", "public/index.html", "data/seed.csv", ".env", "db/schema.sql"])
def test_project_paths_that_are_allowed(path: str) -> None:
    assert is_node_path(path)


@pytest.mark.parametrize(
    "path",
    ["../x.js", "/etc/passwd", "a/../x.js", "node_modules/express/index.js", "src/node_modules/x.js", ".hidden/x.js", "a\\b.js",
     "main.exe", "a b.js", "", "x/" * 8 + "a.js", "a" * 201 + ".js", "C:/x.js"],
)
def test_project_paths_that_are_refused(path: str) -> None:
    assert not is_node_path(path)


def test_valid_requests_become_the_playground_bodies() -> None:
    assert NodeRunRequest.model_validate(script()).to_playground_body() == {"files": [MAIN], "entry": "main.js", "stdin": "Asha\n"}
    parsed = NodeProjectRunRequest.model_validate(project(requests=[{"method": "post", "path": "/api/tasks", "headers": {"Content-Type": "application/json"}, "body": "{}"}]))
    assert parsed.to_playground_body() == {
        "files": [SERVER],
        "entry": "server.js",
        "requests": [{"method": "POST", "path": "/api/tasks", "headers": {"Content-Type": "application/json"}, "body": "{}"}],
    }


@pytest.mark.parametrize(
    "change",
    [
        {"files": []},
        {"files": [MAIN, {"path": "node_modules/x.js", "content": ""}]},
        {"files": [MAIN, MAIN]},
        {"files": [{"path": "main.js", "content": "a" * 262145}]},
        {"files": [{"path": f"f{i}.js", "content": ""} for i in range(41)]},
        {"entry": "other.js"},
        {"entry": "data.json", "files": [MAIN, {"path": "data.json", "content": "{}"}]},
        {"stdin": "a" * 16385},
    ],
)
def test_bad_scripts_are_rejected_before_a_run(change: dict) -> None:
    with pytest.raises(ValidationError):
        NodeRunRequest.model_validate(script(**change))


@pytest.mark.parametrize(
    "change",
    [
        {"entry": "main.js"},
        {"requests": [{"method": "TRACE", "path": "/"}]},
        {"requests": [{"path": "//evil.example/"}]},
        {"requests": [{"path": "/"}] * 31},
    ],
)
def test_bad_projects_are_rejected_before_a_run(change: dict) -> None:
    with pytest.raises(ValidationError):
        NodeProjectRunRequest.model_validate(project(**change))


def test_a_project_and_requests_too_large_together_are_rejected() -> None:
    big = {"path": "data.json", "content": "a" * 200_000}
    requests = [{"method": "POST", "path": "/api/tasks", "body": "b" * 60_000} for _ in range(6)]
    with pytest.raises(ValidationError, match="too large together"):
        NodeProjectRunRequest.model_validate(project(files=[SERVER, big], requests=requests))
    NodeProjectRunRequest.model_validate(project(files=[SERVER, big]))


# ---- Forwarding ------------------------------------------------------------------------------------


@runs
async def test_scripts_and_projects_go_to_their_routes_with_our_token(fresh_limits) -> None:
    service, sent = make_service(ok)
    result = await service.run_script(NodeRunRequest.model_validate(script()), USER)
    assert json.loads(result)["status"] == "success"
    await service.run_project(NodeProjectRunRequest.model_validate(project()), USER)
    assert [str(request.url) for request in sent] == [f"{URL}/api/node/run", f"{URL}/api/node/project/run"]
    assert all(request.headers["Authorization"] == "Bearer google-id-token" for request in sent)
    assert json.loads(sent[0].content)["stdin"] == "Asha\n"
    assert json.loads(sent[1].content)["entry"] == "server.js"
    assert fresh_limits.calls[0] == ('{"type": "service_account"}', URL)


@runs
async def test_the_spring_service_and_keys_are_used_when_node_has_none(fresh_limits) -> None:
    service, sent = make_service(ok, node_playground_url="", node_playground_invoker_key="", spring_playground_url="https://spring.example.run.app", java_playground_invoker_key="java-key")
    await service.run_script(NodeRunRequest.model_validate(script()), USER)
    assert str(sent[0].url) == "https://spring.example.run.app/api/node/run"
    assert fresh_limits.calls[0][0] == "java-key"


@pytest.mark.parametrize("url, key", [("", "key"), ("http://insecure.example", "key"), (URL, "")])
@runs
async def test_an_unconfigured_runner_says_so_and_sends_nothing(url: str, key: str) -> None:
    service, sent = make_service(ok, node_playground_url=url, node_playground_invoker_key=key)
    with pytest.raises(ExternalServiceError, match="not configured"):
        await service.run_script(NodeRunRequest.model_validate(script()), USER)
    assert sent == []


@pytest.mark.parametrize(
    "status, payload, error, text",
    [
        (429, {"error": "busy"}, RateLimitedError, "busy"),
        (400, {"error": "entry must be one of the files"}, BadRequestError, "entry must be one of the files"),
        (413, {"error": "Request exceeds 512 KiB"}, PayloadTooLargeError, "512 KiB"),
        (500, {"error": "boom"}, ExternalServiceError, "unavailable"),
        (404, {"error": "Not found"}, ExternalServiceError, "unavailable"),
    ],
)
@runs
async def test_playground_errors_become_clean_errors(status: int, payload: dict, error: type, text: str) -> None:
    service, _ = make_service(lambda _r: httpx.Response(status, json=payload))
    with pytest.raises(error, match=text):
        await service.run_script(NodeRunRequest.model_validate(script()), USER)


@runs
async def test_a_refused_token_is_dropped(fresh_limits) -> None:
    service, _ = make_service(lambda _r: httpx.Response(403, json={}))
    with pytest.raises(ExternalServiceError):
        await service.run_project(NodeProjectRunRequest.model_validate(project()), USER)
    assert fresh_limits.resets == 1


@pytest.mark.parametrize("handler", [lambda _r: httpx.Response(200, content=b"not json"), lambda _r: httpx.Response(200, json=["x"])])
@runs
async def test_a_strange_answer_is_unavailable_not_a_crash(handler) -> None:
    service, _ = make_service(handler)
    with pytest.raises(ExternalServiceError, match="unavailable"):
        await service.run_script(NodeRunRequest.model_validate(script()), USER)


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("slow"), httpx.ConnectError("down")])
@runs
async def test_timeouts_and_network_errors_are_unavailable(failure: Exception) -> None:
    def handler(_r):
        raise failure

    service, _ = make_service(handler)
    with pytest.raises(ExternalServiceError, match="unavailable"):
        await service.run_script(NodeRunRequest.model_validate(script()), USER)


@runs
async def test_missing_packages_are_logged_by_name(caplog) -> None:
    service, _ = make_service(lambda _r: httpx.Response(200, json={"status": "package_error", "phase": "install", "missing": ["left-pad", "@scope/x"]}))
    with caplog.at_level("INFO", logger="webnest.node_playground"):
        await service.run_script(NodeRunRequest.model_validate(script()), USER)
    assert "missing=left-pad,@scope/x" in caplog.text
    assert "console.log" not in caplog.text


@runs
async def test_odd_missing_names_never_reach_the_log(caplog) -> None:
    names = ["ok-pkg", "evil\nFAKE LOG LINE", 7, None, "x" * 81, {"a": 1}]
    service, _ = make_service(lambda _r: httpx.Response(200, json={"status": "package_error", "missing": names}))
    with caplog.at_level("INFO", logger="webnest.node_playground"):
        await service.run_script(NodeRunRequest.model_validate(script()), USER)
    assert "missing=ok-pkg" in caplog.text
    assert "FAKE LOG LINE" not in caplog.text
    service, _ = make_service(lambda _r: httpx.Response(200, json={"status": "success", "missing": "not a list"}))
    with caplog.at_level("INFO", logger="webnest.node_playground"):
        await service.run_script(NodeRunRequest.model_validate(script()), USER)


# ---- Limits ------------------------------------------------------------------------------------------


@runs
async def test_scripts_and_projects_share_the_per_minute_limit() -> None:
    service, sent = make_service(ok, node_playground_per_minute=3)
    for _ in range(2):
        await service.run_script(NodeRunRequest.model_validate(script()), USER)
    await service.run_project(NodeProjectRunRequest.model_validate(project()), USER)
    with pytest.raises(RateLimitedError, match="in a minute"):
        await service.run_script(NodeRunRequest.model_validate(script()), USER)
    await service.run_script(NodeRunRequest.model_validate(script()), SimpleNamespace(id=uuid.uuid4()))
    assert len(sent) == 4


@runs
async def test_the_per_day_limit_comes_from_the_settings() -> None:
    service, sent = make_service(ok, node_playground_per_day=1)
    await service.run_script(NodeRunRequest.model_validate(script()), USER)
    with pytest.raises(RateLimitedError, match="today's Node.js run limit"):
        await service.run_script(NodeRunRequest.model_validate(script()), USER)
    assert len(sent) == 1


@runs
async def test_a_busy_playground_does_not_use_up_any_limit() -> None:
    usage = FakeUsage()
    service, sent = make_service(lambda _r: httpx.Response(429, json={"error": "busy"}), usage=usage, node_playground_per_minute=2)
    for _ in range(4):
        with pytest.raises(RateLimitedError, match="busy"):
            await service.run_script(NodeRunRequest.model_validate(script()), USER)
    assert len(sent) == 4
    assert usage.runs == 0


@runs
async def test_the_site_wide_daily_cap_pauses_runs() -> None:
    service, sent = make_service(ok, usage=FakeUsage(start=3000))
    with pytest.raises(RateLimitedError, match="paused for today"):
        await service.run_script(NodeRunRequest.model_validate(script()), USER)
    assert sent == []


@runs
async def test_without_the_counter_runs_fail_closed() -> None:
    service, sent = make_service(ok, usage=FakeUsage(fail=True))
    with pytest.raises(ExternalServiceError, match="unavailable"):
        await service.run_script(NodeRunRequest.model_validate(script()), USER)
    assert sent == []


# ---- Package list ------------------------------------------------------------------------------------


@runs
async def test_the_package_list_is_fetched_once_and_never_counts_as_a_run() -> None:
    listed = {"nodeVersion": "v24.0.0", "packages": [{"name": "express", "version": "5.0.0"}]}
    usage = FakeUsage()
    service, sent = make_service(lambda _r: httpx.Response(200, json=listed), usage=usage)
    assert json.loads(await service.packages()) == listed
    assert json.loads(await service.packages()) == listed
    assert len(sent) == 1
    assert str(sent[0].url) == f"{URL}/api/node/packages"
    assert usage.runs == 0


@pytest.mark.parametrize(
    "handler",
    [
        lambda _r: httpx.Response(404, json={}),
        lambda _r: httpx.Response(200, json={"x": 1}),
        lambda _r: httpx.Response(200, content=b"not json"),
    ],
)
@runs
async def test_a_bad_package_list_is_unavailable_and_retried_only_after_a_minute(handler, monkeypatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr("services.node_playground_service.time.monotonic", lambda: clock[0])
    service, sent = make_service(handler)
    for _ in range(3):
        with pytest.raises(ExternalServiceError, match="unavailable"):
            await service.packages()
    # A public page cannot make the runner answer on every view while it is down.
    assert len(sent) == 1
    clock[0] += 61
    with pytest.raises(ExternalServiceError):
        await service.packages()
    assert len(sent) == 2


@runs
async def test_an_unreachable_runner_gives_a_clean_package_error(fresh_limits) -> None:
    def down(_r):
        raise httpx.ConnectError("down")

    service, _ = make_service(down)
    with pytest.raises(ExternalServiceError, match="unavailable"):
        await service.packages()


@runs
async def test_the_package_list_is_fetched_again_after_an_hour(monkeypatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr("services.node_playground_service.time.monotonic", lambda: clock[0])
    service, sent = make_service(lambda _r: httpx.Response(200, json={"packages": []}))
    await service.packages()
    clock[0] += 3599
    await service.packages()
    assert len(sent) == 1
    clock[0] += 2
    await service.packages()
    assert len(sent) == 2


@runs
async def test_concurrent_package_lookups_share_one_call() -> None:
    service, sent = make_service(lambda _r: httpx.Response(200, json={"packages": []}))
    await asyncio.gather(*(service.packages() for _ in range(5)))
    assert len(sent) == 1


# ---- Routes --------------------------------------------------------------------------------------------


class FakeService:
    def __init__(self) -> None:
        self.calls = []

    async def run_script(self, payload, user):
        self.calls.append(("script", payload, user))
        return b'{"status":"success"}'

    async def run_project(self, payload, user):
        self.calls.append(("project", payload, user))
        return b'{"status":"success","responses":[]}'

    async def packages(self):
        return b'{"packages":[]}'


@pytest.fixture
def client():
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.mark.parametrize("path, payload", [("/api/node/run", script()), ("/api/node/project/run", project())])
def test_runs_need_a_login(client, path: str, payload: dict) -> None:
    fake = FakeService()
    app.dependency_overrides[get_node_playground_service] = lambda: fake
    assert client.post(path, json=payload).status_code == 401
    assert fake.calls == []


def test_the_routes_pass_the_playground_answer_through(client) -> None:
    fake = FakeService()
    app.dependency_overrides[get_node_playground_service] = lambda: fake
    app.dependency_overrides[get_current_user] = lambda: USER
    headers = {"Authorization": "Bearer x"}
    assert client.post("/api/node/run", json=script(), headers=headers).json() == {"status": "success"}
    assert client.post("/api/node/project/run", json=project(), headers=headers).json() == {"status": "success", "responses": []}
    assert [call[0] for call in fake.calls] == ["script", "project"]
    assert client.post("/api/node/run", json=script(entry="nope.js"), headers=headers).status_code == 422
    assert len(fake.calls) == 2


def test_the_package_list_is_public_and_cacheable(client) -> None:
    app.dependency_overrides[get_node_playground_service] = lambda: FakeService()
    response = client.get("/api/node/packages")
    assert response.status_code == 200
    assert response.json() == {"packages": []}
    assert "max-age=3600" in response.headers["cache-control"]


# ---- WebNest AI ----------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["ask", "agent"])
def test_every_assistant_language_has_a_prompt_and_tools(mode: str) -> None:
    # A language the schema accepts but the prompts lack would fail every chat with a KeyError.
    from typing import get_args

    from schemas.assistant_schemas import AssistantLanguage
    from services.assistant_prompts import system_prompt, tools_for

    for language in get_args(AssistantLanguage):
        assert system_prompt(mode, language)
        assert isinstance(tools_for(mode, language), list)
