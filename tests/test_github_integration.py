"""CodeLab GitHub sync: contract error mapping, input validation, snapshot
planning and the commit flow, against an in-memory fake of the GitHub API.

No network and no database: GitHub is an httpx.MockTransport and the
persistence layer is a small in-memory fake.
"""

import asyncio
import base64
import functools
import hashlib
import json
import os
import subprocess
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

os.environ.setdefault("SUPABASE_URL", "postgresql://user:pass@localhost:5432/test")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError as PydanticValidationError

from core.dependencies import get_current_user, get_github_service
from core.error_handlers import register_error_handlers
from core.exceptions import GitHubError
from schemas.github_schemas import CommitRequest, OAuthStartRequest, RepoCreateRequest, validate_branch
from services import github_client as github_client_module
from services import github_service as github_service_module
from services.github_client import GitHubClient
from services.github_service import (
    MAX_FILES,
    GitHubService,
    git_blob_sha,
    guess_mode,
    plan_snapshot,
)

HEAD = "a" * 40
TREE = "b" * 40
NEW_TREE = "c" * 40
NEW_COMMIT = "d" * 40
USER = SimpleNamespace(id=uuid.uuid4(), role="client")


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch):
    monkeypatch.setattr(github_client_module, "RETRY_DELAYS", (0, 0))


def settings(**overrides):
    values = dict(
        github_client_id="client-id",
        github_client_secret="client-secret",
        github_redirect_uri="https://www.webneststudio.co.in/codelab/github/callback",
        github_token_encryption_key="",
        github_api_timeout_seconds=5.0,
        jwt_secret_key="test-secret",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeStore:
    """Mirrors GitHubPersistence."""

    def __init__(self) -> None:
        self.connection = None
        self.states: dict[str, dict] = {}
        self.deleted = False

    async def get_connection(self, user_id):
        return self.connection

    async def release(self):
        pass

    shared = False

    async def other_users_linked_to(self, github_user_id, user_id):
        return self.shared

    async def delete_connection(self, user_id):
        self.connection = None
        self.deleted = True

    async def create_state(self, user_id, state_hash, return_to, ttl):
        self.states[state_hash] = {"user_id": user_id, "return_to": return_to, "used": False}

    async def consume_state(self, user_id, state_hash):
        row = self.states.get(state_hash)
        if row is None or row["used"] or row["user_id"] != user_id:
            return None
        row["used"] = True
        return row["return_to"]


def make_service(monkeypatch, handler, *, connected=True, **setting_overrides):
    service = GitHubService(session=None, settings=settings(**setting_overrides))
    store = FakeStore()
    if connected:
        store.connection = SimpleNamespace(
            access_token_encrypted=service._fernet().encrypt(b"gho_token").decode(),
            github_user_id=1,
            login="octocat",
            avatar_url=None,
            scopes=["repo"],
            connected_at=datetime.now(timezone.utc),
        )
    service._github = store
    store.revoked = []

    store.revoked_tokens = []

    async def fake_revoke(client_id, client_secret, token, timeout):
        store.revoked.append(token)

    async def fake_revoke_token(client_id, client_secret, token, timeout):
        store.revoked_tokens.append(token)

    monkeypatch.setattr(
        github_service_module, "GitHubClient", functools.partial(GitHubClient, transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(github_service_module, "revoke_grant", fake_revoke)
    monkeypatch.setattr(github_service_module, "revoke_token", fake_revoke_token)
    return service, store


class FakeGitHub:
    """Just enough of the Git Data API for one repository octocat/demo."""

    def __init__(self, *, head=HEAD, ref_update_status=200) -> None:
        self.head = head
        self.ref_update_status = ref_update_status
        self.calls: list[tuple[str, str]] = []
        self.tree_payload = None
        self.base_tree = [
            {"path": "run.sh", "mode": "100755", "type": "blob", "sha": "1" * 40},
            {"path": "old.py", "mode": "100644", "type": "blob", "sha": "2" * 40},
        ]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer gho_token"
        path = request.url.path
        self.calls.append((request.method, path))
        base = "/repos/octocat/demo"
        if request.method == "GET" and path == f"{base}/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": self.head}})
        if request.method == "GET" and path == f"{base}/git/commits/{HEAD}":
            return httpx.Response(200, json={"tree": {"sha": TREE}})
        if request.method == "GET" and path == f"{base}/git/trees/{TREE}":
            return httpx.Response(200, json={"tree": self.base_tree, "truncated": False})
        if request.method == "POST" and path == f"{base}/git/trees":
            self.tree_payload = json.loads(request.content)
            return httpx.Response(201, json={"sha": NEW_TREE})
        if request.method == "POST" and path == f"{base}/git/commits":
            return httpx.Response(201, json={"sha": NEW_COMMIT, "html_url": f"https://github.com/octocat/demo/commit/{NEW_COMMIT}"})
        if request.method == "PATCH" and path == f"{base}/git/refs/heads/main":
            if self.ref_update_status != 200:
                self.head = "e" * 40
                return httpx.Response(self.ref_update_status, json={"message": "Update is not a fast forward"})
            return httpx.Response(200, json={"object": {"sha": NEW_COMMIT}})
        return httpx.Response(404, json={"message": "Not Found"})


def commit_request(**overrides) -> CommitRequest:
    body = {
        "branch": "main",
        "base_commit_sha": HEAD,
        "message": "Update",
        "changes": [
            {"path": "run.sh", "action": "upsert", "content": "echo hi\n"},
            {"path": "src/new.py", "action": "upsert", "content": "print('new')\n"},
            {"path": "old.py", "action": "delete"},
            {"path": "never-existed.py", "action": "delete"},
        ],
    }
    body.update(overrides)
    return CommitRequest(**body)


# --------------------------------------------------------------------------- #
# Blob SHA
# --------------------------------------------------------------------------- #
def test_git_blob_sha_matches_git():
    assert git_blob_sha(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"
    assert git_blob_sha(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"


def test_git_blob_sha_matches_git_hash_object_when_available():
    data = "naïve – ünïcode\r\nline two\n".encode()
    try:
        expected = subprocess.run(
            ["git", "hash-object", "--stdin"], input=data, capture_output=True, check=True
        ).stdout.decode().strip()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git not available")
    assert git_blob_sha(data) == expected


# --------------------------------------------------------------------------- #
# Commits
# --------------------------------------------------------------------------- #
def test_commit_writes_tree_commit_and_fast_forward_ref(monkeypatch):
    fake = FakeGitHub()
    service, _ = make_service(monkeypatch, fake)
    result = asyncio.run(service.commit(USER, "octocat", "demo", commit_request()))

    assert result.commit_sha == NEW_COMMIT
    assert {f.path: f.sha for f in result.files} == {
        "run.sh": git_blob_sha(b"echo hi\n"),
        "src/new.py": git_blob_sha(b"print('new')\n"),
    }
    entries = {e["path"]: e for e in fake.tree_payload["tree"]}
    assert fake.tree_payload["base_tree"] == TREE
    assert entries["run.sh"]["mode"] == "100755"  # executable bit kept
    assert entries["src/new.py"]["mode"] == "100644"
    assert entries["old.py"]["sha"] is None
    assert "never-existed.py" not in entries
    patch = [c for c in fake.calls if c[0] == "PATCH"]
    assert patch == [("PATCH", "/repos/octocat/demo/git/refs/heads/main")]


def test_commit_on_stale_base_is_rejected_without_writing(monkeypatch):
    fake = FakeGitHub(head="f" * 40)
    service, _ = make_service(monkeypatch, fake)
    with pytest.raises(GitHubError) as exc:
        asyncio.run(service.commit(USER, "octocat", "demo", commit_request()))
    assert (exc.value.status_code, exc.value.code) == (409, "non_fast_forward")
    assert exc.value.extra == {"remote_commit_sha": "f" * 40}
    assert all(method == "GET" for method, _ in fake.calls)


def test_commit_losing_a_race_on_ref_update_is_non_fast_forward(monkeypatch):
    fake = FakeGitHub(ref_update_status=422)
    service, _ = make_service(monkeypatch, fake)
    with pytest.raises(GitHubError) as exc:
        asyncio.run(service.commit(USER, "octocat", "demo", commit_request()))
    assert exc.value.code == "non_fast_forward"
    assert exc.value.extra == {"remote_commit_sha": "e" * 40}


def test_commit_limits_are_413(monkeypatch):
    service, _ = make_service(monkeypatch, FakeGitHub())
    many = [{"path": f"f{i}.py", "action": "upsert", "content": "x"} for i in range(301)]
    with pytest.raises(GitHubError) as exc:
        asyncio.run(service.commit(USER, "octocat", "demo", commit_request(changes=many)))
    assert (exc.value.status_code, exc.value.code) == (413, "payload_too_large")

    big = [{"path": f"f{i}.py", "action": "upsert", "content": "x" * 800_000} for i in range(3)]
    with pytest.raises(GitHubError) as exc:
        asyncio.run(service.commit(USER, "octocat", "demo", commit_request(changes=big)))
    assert exc.value.code == "payload_too_large"


def test_commit_request_validation():
    bad_paths = ["/abs.py", "../up.py", "a/../b.py", "a//b.py", "a\\b.py", ".git/config", "x/.GIT/hooks/pre", "a\x00b", "./a.py", "dir/"]
    for path in bad_paths:
        with pytest.raises(PydanticValidationError):
            commit_request(changes=[{"path": path, "action": "delete"}])
    with pytest.raises(PydanticValidationError):
        commit_request(changes=[{"path": "a.py", "action": "upsert"}])  # content required
    with pytest.raises(PydanticValidationError):
        commit_request(changes=[{"path": "a.py", "action": "delete", "content": ""}])  # content forbidden
    with pytest.raises(PydanticValidationError):
        commit_request(changes=[])
    with pytest.raises(PydanticValidationError):
        commit_request(message="x" * 201)
    with pytest.raises(PydanticValidationError):
        commit_request(base_commit_sha="not-a-sha")
    with pytest.raises(PydanticValidationError):
        commit_request(changes=[{"path": "a.py", "action": "delete"}, {"path": "a.py", "action": "delete"}])


# --------------------------------------------------------------------------- #
# Error mapping
# --------------------------------------------------------------------------- #
def test_revoked_token_is_409_reauth_and_forgets_the_connection(monkeypatch):
    service, store = make_service(monkeypatch, lambda request: httpx.Response(401, json={"message": "Bad credentials"}))
    with pytest.raises(GitHubError) as exc:
        asyncio.run(service.list_branches(USER, "octocat", "demo"))
    assert (exc.value.status_code, exc.value.code) == (409, "github_reauth_required")
    assert store.deleted


def test_rate_limit_is_429_with_retry_after(monkeypatch):
    def handler(request):
        return httpx.Response(403, headers={"x-ratelimit-remaining": "0", "retry-after": "42"}, json={"message": "API rate limit exceeded"})

    service, _ = make_service(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        asyncio.run(service.list_branches(USER, "octocat", "demo"))
    assert (exc.value.status_code, exc.value.code, exc.value.extra) == (429, "github_rate_limited", {"retry_after": 42})


def test_missing_repo_and_server_errors(monkeypatch):
    service, _ = make_service(monkeypatch, lambda request: httpx.Response(404, json={"message": "Not Found"}))
    with pytest.raises(GitHubError) as exc:
        asyncio.run(service.list_branches(USER, "octocat", "demo"))
    assert (exc.value.status_code, exc.value.code) == (404, "repo_not_found")

    service, _ = make_service(monkeypatch, lambda request: httpx.Response(503))
    with pytest.raises(GitHubError) as exc:
        asyncio.run(service.list_branches(USER, "octocat", "demo"))
    assert (exc.value.status_code, exc.value.code) == (502, "github_unavailable")

    def boom(request):
        raise httpx.ConnectError("down")

    service, _ = make_service(monkeypatch, boom)
    with pytest.raises(GitHubError) as exc:
        asyncio.run(service.list_branches(USER, "octocat", "demo"))
    assert exc.value.code == "github_unavailable"


def test_not_connected_is_409(monkeypatch):
    service, _ = make_service(monkeypatch, FakeGitHub(), connected=False)
    with pytest.raises(GitHubError) as exc:
        asyncio.run(service.list_branches(USER, "octocat", "demo"))
    assert (exc.value.status_code, exc.value.code) == (409, "github_not_connected")


def test_owner_and_repo_never_reach_github_unvalidated(monkeypatch):
    fake = FakeGitHub()
    service, _ = make_service(monkeypatch, fake)
    for owner, repo in [("..", "demo"), ("octocat", ".."), ("octo/cat", "demo"), ("octocat", "de mo"), ("octocat", "a?b")]:
        with pytest.raises(GitHubError) as exc:
            asyncio.run(service.list_branches(USER, owner, repo))
        assert exc.value.code == "repo_not_found"
    assert fake.calls == []


def test_token_is_stored_encrypted(monkeypatch):
    service, store = make_service(monkeypatch, FakeGitHub())
    assert "gho_token" not in store.connection.access_token_encrypted
    # A changed key makes the stored token unreadable: reconnect, not a crash.
    other = GitHubService(session=None, settings=settings(jwt_secret_key="rotated"))
    other._github = store
    with pytest.raises(GitHubError) as exc:
        asyncio.run(other._token(USER))
    assert exc.value.code == "github_reauth_required"
    assert store.deleted


# --------------------------------------------------------------------------- #
# OAuth
# --------------------------------------------------------------------------- #
def test_oauth_state_is_single_use_and_user_bound(monkeypatch):
    service, store = make_service(monkeypatch, FakeGitHub(), connected=False)
    start = asyncio.run(service.start_oauth(USER, "/codelab/playground"))
    assert start.authorize_url.startswith("https://github.com/login/oauth/authorize?")
    state = httpx.URL(start.authorize_url).params["state"]
    assert hashlib.sha256(state.encode()).hexdigest() in store.states
    assert state not in store.states  # only the hash is stored

    other_user = SimpleNamespace(id=uuid.uuid4())
    with pytest.raises(GitHubError) as exc:
        asyncio.run(service.complete_oauth(other_user, "code", state))
    assert (exc.value.status_code, exc.value.code) == (400, "invalid_state")

    with pytest.raises(GitHubError) as exc:
        asyncio.run(service.complete_oauth(USER, "code", "forged"))
    assert exc.value.code == "invalid_state"


def test_return_to_must_stay_under_codelab():
    for value in ["/codelab/playground", "/codelab/playground?mode=java"]:
        OAuthStartRequest(return_to=value)
    for value in ["https://evil.example/codelab/", "//evil.example/codelab/", "/codelab//evil.example", "/login", "/codelab/../admin", "/codelab/\\evil", "javascript:alert(1)"]:
        with pytest.raises(PydanticValidationError):
            OAuthStartRequest(return_to=value)


def test_repo_name_and_branch_validation():
    RepoCreateRequest(name="todo-api.v2_x")
    for name in ["", "a b", "a/b", "x" * 101, "..", "ä"]:
        with pytest.raises(PydanticValidationError):
            RepoCreateRequest(name=name)
    assert validate_branch("feature/login") == "feature/login"
    for branch in ["../main", "a..b", "-x", "a b", "a~1", "x.lock", "/main", "a//b", "@{x}", ".hidden"]:
        with pytest.raises(ValueError):
            validate_branch(branch)


# --------------------------------------------------------------------------- #
# Snapshot
# --------------------------------------------------------------------------- #
def blob(path, size=10, mode="100644"):
    return {"path": path, "type": "blob", "size": size, "mode": mode, "sha": hashlib.sha1(path.encode()).hexdigest()}


def test_plan_snapshot_applies_limits():
    entries = [
        blob("app/main.py"),
        blob("logo.png"),
        blob("big.json", size=300 * 1024),
        blob("node_modules/a/index.js"),
        blob("node_modules/b/index.js"),
        blob("link", mode="120000"),
        {"path": "app", "type": "tree"},
    ] + [blob(f"src/f{i:03}.py") for i in range(MAX_FILES)]
    selected, skipped = plan_snapshot(entries)
    reasons = {s.path: s.reason for s in skipped}
    assert len(selected) == MAX_FILES
    assert reasons["logo.png"] == "binary"
    assert reasons["link"] == "binary"
    assert reasons["big.json"] == "too_large"
    assert reasons["node_modules/"] == "ignored_dir"
    assert sum(1 for s in skipped if s.reason == "ignored_dir") == 1
    assert sum(1 for s in skipped if s.reason == "limit") == 1


def test_plan_snapshot_total_size_limit():
    entries = [blob(f"f{i}.txt", size=150 * 1024) for i in range(20)]
    selected, skipped = plan_snapshot(entries)
    assert sum(e["size"] for e in selected) <= 2 * 1024 * 1024
    assert {s.reason for s in skipped} == {"limit"}


def test_guess_mode():
    assert guess_mode([("Main.java", ""), ("pom.xml", "")]) == "java"
    assert guess_mode([("app/main.py", "from fastapi import FastAPI\n")]) == "backend"
    assert guess_mode([("main.py", "x = 1"), ("requirements.txt", "Flask==3.0\n")]) == "backend"
    assert guess_mode([("main.py", "print(1)")]) == "python"
    assert guess_mode([("index.html", ""), ("style.css", "")]) == "web"
    assert guess_mode([("server.js", ""), ("package.json", '{"dependencies": {"express": "4"}}')]) == "backend"


def test_snapshot_returns_text_files_and_skips_binary_content(monkeypatch):
    text_entry = blob("main.py")
    binary_entry = blob("data.txt")
    contents = {text_entry["sha"]: b"print('hi')\n", binary_entry["sha"]: b"\x00\x01\x02"}

    def handler(request):
        base = "/repos/octocat/demo"
        path = request.url.path
        if path == base:
            return httpx.Response(200, json={"default_branch": "main"})
        if path == f"{base}/branches/main":
            return httpx.Response(200, json={"commit": {"sha": HEAD}})
        if path == f"{base}/git/trees/{HEAD}":
            return httpx.Response(200, json={"tree": [text_entry, binary_entry], "truncated": False})
        sha = path.rsplit("/", 1)[-1]
        return httpx.Response(200, json={"content": base64.b64encode(contents[sha]).decode(), "encoding": "base64"})

    service, _ = make_service(monkeypatch, handler)
    snap = asyncio.run(service.snapshot(USER, "octocat", "demo", None, "auto"))
    assert snap.branch == "main" and snap.commit_sha == HEAD and snap.mode == "python"
    assert [(f.path, f.language, f.content, f.sha) for f in snap.files] == [
        ("main.py", "python", "print('hi')\n", text_entry["sha"])
    ]
    assert [(s.path, s.reason) for s in snap.skipped] == [("data.txt", "binary")]


# --------------------------------------------------------------------------- #
# HTTP layer: response shapes
# --------------------------------------------------------------------------- #
def http_client(service) -> TestClient:
    from api.github_router import router

    app = FastAPI()
    register_error_handlers(app)
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: USER
    app.dependency_overrides[get_github_service] = lambda: service
    return TestClient(app)


def test_error_body_and_validation_shape(monkeypatch):
    service, _ = make_service(monkeypatch, FakeGitHub(head="f" * 40))
    client = http_client(service)

    response = client.post(
        "/api/github/repos/octocat/demo/commits",
        json={"branch": "main", "base_commit_sha": HEAD, "message": "m", "changes": [{"path": "a.py", "action": "upsert", "content": "x"}]},
    )
    assert response.status_code == 409
    assert response.json() == {
        "detail": "GitHub has newer commits on this branch. Pull first.",
        "code": "non_fast_forward",
        "remote_commit_sha": "f" * 40,
    }

    secret = "TOP-SECRET-FILE-CONTENT"
    response = client.post(
        "/api/github/repos/octocat/demo/commits",
        json={"branch": "main", "base_commit_sha": HEAD, "message": "", "changes": [{"path": "/abs.py", "action": "upsert", "content": secret}]},
    )
    assert response.status_code == 422
    assert secret not in response.text  # input never echoed
    for error in response.json()["errors"]:
        assert set(error) == {"loc", "msg"} and error["loc"][0] == "body"

    response = client.post("/api/github/oauth/start", json={"return_to": "https://evil.example"})
    assert response.status_code == 422
    assert response.json()["errors"][0]["loc"] == ["body", "return_to"]


def test_disconnect_is_idempotent(monkeypatch):
    service, store = make_service(monkeypatch, lambda request: httpx.Response(204))
    client = http_client(service)
    assert client.delete("/api/github/connection").status_code == 204
    assert store.connection is None
    assert store.revoked == ["gho_token"]
    assert client.delete("/api/github/connection").status_code == 204
    assert client.get("/api/github/connection").json() == {
        "connected": False, "login": None, "avatar_url": None, "scopes": [], "connected_at": None
    }


def test_oversized_github_request_is_413_before_parsing():
    from core.request_limits import GitHubBodyLimitMiddleware

    app = FastAPI()

    @app.post("/api/github/repos")
    async def endpoint():
        raise AssertionError("body limit should have answered")

    app.add_middleware(GitHubBodyLimitMiddleware, max_bytes=100)
    response = TestClient(app).post("/api/github/repos", content=b"x" * 101, headers={"content-type": "application/json"})
    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"


@pytest.mark.parametrize(
    "body",
    [[{"unexpected": 1}], {"not": "a list"}, [None], "plain text"],
)
def test_malformed_github_response_is_502_not_a_crash(monkeypatch, body):
    def handler(request):
        if body == "plain text":
            return httpx.Response(200, text="<html>")
        return httpx.Response(200, json=body)

    service, _ = make_service(monkeypatch, handler)
    list_calls = (
        lambda: service.list_branches(USER, "octocat", "demo"),
        lambda: service.list_repos(USER, 1, 30, None),
        lambda: service.list_repos(USER, 1, 30, "demo"),
    )
    other_calls = (
        lambda: service.snapshot(USER, "octocat", "demo", "main", "auto"),
        lambda: service.commit(USER, "octocat", "demo", commit_request()),
    )
    for call in list_calls:
        if isinstance(body, list):
            # Malformed items are skipped; the list itself still works.
            assert asyncio.run(call()).items == []
        else:
            with pytest.raises(GitHubError) as exc:
                asyncio.run(call())
            assert (exc.value.status_code, exc.value.code) == (502, "github_unavailable")
    for call in other_calls:
        with pytest.raises(GitHubError) as exc:
            asyncio.run(call())
        assert (exc.value.status_code, exc.value.code) == (502, "github_unavailable")


def test_malformed_profile_during_oauth_is_502(monkeypatch):
    service, store = make_service(monkeypatch, lambda request: httpx.Response(200, json={"no_id": True}), connected=False)

    async def fake_exchange(*args):
        return {"access_token": "gho_token", "scope": "repo"}

    monkeypatch.setattr(github_service_module, "exchange_oauth_code", fake_exchange)
    start = asyncio.run(service.start_oauth(USER, "/codelab/playground"))
    state = httpx.URL(start.authorize_url).params["state"]
    with pytest.raises(GitHubError) as exc:
        asyncio.run(service.complete_oauth(USER, "code", state))
    assert exc.value.code == "github_unavailable"
    assert store.connection is None
    assert store.revoked_tokens == ["gho_token"]  # never stored, so revoked now


def test_token_without_repo_scope_is_refused_and_revoked(monkeypatch):
    service, store = make_service(monkeypatch, lambda request: httpx.Response(200, json={"id": 7, "login": "octocat"}), connected=False)

    async def fake_exchange(*args):
        return {"access_token": "gho_new", "scope": "public_repo"}

    monkeypatch.setattr(github_service_module, "exchange_oauth_code", fake_exchange)
    state = httpx.URL(asyncio.run(service.start_oauth(USER, "/codelab/playground")).authorize_url).params["state"]
    with pytest.raises(GitHubError) as exc:
        asyncio.run(service.complete_oauth(USER, "code", state))
    assert (exc.value.status_code, exc.value.code) == (400, "oauth_exchange_failed")
    assert store.connection is None
    assert store.revoked_tokens == ["gho_new"]


# --------------------------------------------------------------------------- #
# Code-review fixes
# --------------------------------------------------------------------------- #
def test_protected_branch_is_not_reported_as_non_fast_forward(monkeypatch):
    fake = FakeGitHub(ref_update_status=422)

    def handler(request):
        response = fake(request)
        fake.head = HEAD  # the head did NOT move: GitHub refused the update itself
        return response

    service, _ = make_service(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        asyncio.run(service.commit(USER, "octocat", "demo", commit_request()))
    assert exc.value.status_code == 422 and exc.value.code is None
    assert "protected" in exc.value.message


class TruncatedGitHub(FakeGitHub):
    """Recursive listing truncated: only per-directory listings are complete."""

    def __call__(self, request):
        base = "/repos/octocat/demo"
        path = request.url.path
        if request.method == "GET" and path == f"{base}/git/trees/{TREE}":
            self.calls.append((request.method, path))
            if request.url.params.get("recursive"):
                return httpx.Response(200, json={"sha": TREE, "tree": [], "truncated": True})
            return httpx.Response(200, json={"sha": TREE, "tree": [
                {"path": "scripts", "mode": "040000", "type": "tree", "sha": "9" * 40},
                {"path": "old.py", "mode": "100644", "type": "blob", "sha": "2" * 40},
            ]})
        if request.method == "GET" and path == f"{base}/git/trees/{'9' * 40}":
            self.calls.append((request.method, path))
            return httpx.Response(200, json={"sha": "9" * 40, "tree": [
                {"path": "deploy.sh", "mode": "100755", "type": "blob", "sha": "3" * 40},
            ]})
        return super().__call__(request)


def test_truncated_base_tree_keeps_exec_bit_and_skips_missing_deletes(monkeypatch):
    fake = TruncatedGitHub()
    service, _ = make_service(monkeypatch, fake)
    request = commit_request(changes=[
        {"path": "scripts/deploy.sh", "action": "upsert", "content": "echo deploy\n"},
        {"path": "old.py", "action": "delete"},
        {"path": "gone/already.py", "action": "delete"},
    ])
    asyncio.run(service.commit(USER, "octocat", "demo", request))
    entries = {e["path"]: e for e in fake.tree_payload["tree"]}
    assert entries["scripts/deploy.sh"]["mode"] == "100755"
    assert entries["old.py"]["sha"] is None
    assert "gone/already.py" not in entries


def test_truncated_snapshot_walks_directories_and_reports_what_it_left(monkeypatch):
    dirs = {f"{i:02}": hashlib.sha1(str(i).encode()).hexdigest() for i in range(5)}
    content = base64.b64encode(b"x = 1\n").decode()

    def handler(request):
        base = "/repos/octocat/demo"
        path = request.url.path
        if path == f"{base}/branches/main":
            return httpx.Response(200, json={"commit": {"sha": HEAD}})
        if path == f"{base}/git/trees/{HEAD}" and request.url.params.get("recursive"):
            return httpx.Response(200, json={"sha": TREE, "tree": [], "truncated": True})
        if path == f"{base}/git/trees/{TREE}":
            listing = [{"path": name, "type": "tree", "sha": sha} for name, sha in dirs.items()]
            listing.append({"path": "node_modules", "type": "tree", "sha": "0" * 40})
            return httpx.Response(200, json={"tree": listing})
        for sha in dirs.values():
            if path == f"{base}/git/trees/{sha}":
                return httpx.Response(200, json={"tree": [{"path": "m.py", "type": "blob", "size": 6, "mode": "100644", "sha": sha}]})
        if "/git/blobs/" in path:
            return httpx.Response(200, json={"content": content})
        return httpx.Response(404)

    monkeypatch.setattr(github_service_module, "MAX_WALK_DIRS", 4)  # root + 3 of the 5 dirs
    service, _ = make_service(monkeypatch, handler)
    snap = asyncio.run(service.snapshot(USER, "octocat", "demo", "main", "auto"))
    assert [f.path for f in snap.files] == ["00/m.py", "01/m.py", "02/m.py"]
    assert {s.path: s.reason for s in snap.skipped} == {"03/": "limit", "04/": "limit", "node_modules/": "ignored_dir"}


def test_guess_mode_python_framework_beats_many_templates():
    files = [(f"app/v{i}.py", "from django.http import HttpResponse\n") for i in range(3)]
    files += [(f"templates/t{i}.html", "") for i in range(20)] + [(f"static/s{i}.js", "") for i in range(20)]
    assert guess_mode(files) == "backend"


def test_first_failed_blob_cancels_the_rest(monkeypatch):
    blob_calls = []
    entries = [blob(f"f{i:03}.py") for i in range(200)]

    async def handler(request):
        base = "/repos/octocat/demo"
        path = request.url.path
        if path == f"{base}/branches/main":
            return httpx.Response(200, json={"commit": {"sha": HEAD}})
        if path == f"{base}/git/trees/{HEAD}":
            return httpx.Response(200, json={"tree": entries, "truncated": False})
        blob_calls.append(path)
        await asyncio.sleep(0.01)  # network latency: lets cancellation happen
        return httpx.Response(403, headers={"x-ratelimit-remaining": "0"}, json={"message": "rate limit"})

    service, _ = make_service(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        asyncio.run(service.snapshot(USER, "octocat", "demo", "main", "auto"))
    assert exc.value.code == "github_rate_limited"
    assert len(blob_calls) < 50


def test_repo_search_stops_once_the_page_is_filled(monkeypatch):
    calls = []
    next_link = {"link": '<https://api.github.com/user/repos?page=9>; rel="next"'}

    def handler(request):
        page = int(request.url.params["page"])
        calls.append(page)
        repos = [
            {"owner": {"login": "o"}, "name": f"app-{page}-{i}", "full_name": f"o/app-{page}-{i}", "private": False,
             "default_branch": "main", "html_url": "https://github.com/o/x", "updated_at": None}
            for i in range(100)
        ]
        return httpx.Response(200, json=repos, headers=next_link)

    service, _ = make_service(monkeypatch, handler)
    result = asyncio.run(service.list_repos(USER, 1, 30, "app"))
    assert len(result.items) == 30 and result.has_more
    assert calls == [1]


def test_reconnect_revokes_only_the_previous_token(monkeypatch):
    service, store = make_service(monkeypatch, lambda request: httpx.Response(200, json={"id": 7, "login": "octocat"}))

    async def fake_exchange(*args):
        return {"access_token": "gho_new", "scope": "repo"}

    async def fake_upsert(user_id, **values):
        replaced = store.connection.access_token_encrypted if store.connection else None
        store.connection = SimpleNamespace(**values, connected_at=datetime.now(timezone.utc))
        return store.connection, replaced

    monkeypatch.setattr(github_service_module, "exchange_oauth_code", fake_exchange)
    store.upsert_connection = fake_upsert
    state = httpx.URL(asyncio.run(service.start_oauth(USER, "/codelab/playground")).authorize_url).params["state"]
    asyncio.run(service.complete_oauth(USER, "code", state))
    assert store.revoked_tokens == ["gho_token"]
    assert store.revoked == []  # the grant itself (and so the new token) is untouched


def test_two_parallel_connect_flows_both_complete(monkeypatch):
    service, store = make_service(monkeypatch, FakeGitHub(), connected=False)
    first = httpx.URL(asyncio.run(service.start_oauth(USER, "/codelab/a")).authorize_url).params["state"]
    second = httpx.URL(asyncio.run(service.start_oauth(USER, "/codelab/b")).authorize_url).params["state"]
    assert asyncio.run(store.consume_state(USER.id, hashlib.sha256(first.encode()).hexdigest())) == "/codelab/a"
    assert asyncio.run(store.consume_state(USER.id, hashlib.sha256(second.encode()).hexdigest())) == "/codelab/b"


def test_create_state_sql_keeps_recent_states():
    from datetime import timedelta

    from sqlalchemy.dialects import postgresql

    from database.github_persistence import MAX_LIVE_STATES, GitHubPersistence

    statements = []

    class Session:
        async def execute(self, statement):
            statements.append(str(statement.compile(dialect=postgresql.dialect())))

        def add(self, row):
            pass

        async def commit(self):
            pass

    asyncio.run(GitHubPersistence(Session()).create_state(USER.id, "h", "/codelab/x", timedelta(minutes=10)))
    assert "NOT IN (SELECT github_oauth_states.state_hash" in statements[0]
    assert "LIMIT" in statements[0] and MAX_LIVE_STATES > 1


@pytest.mark.parametrize("body", [[1, 2], "text", 5])
def test_non_object_error_bodies_still_map_correctly(monkeypatch, body):
    service, _ = make_service(monkeypatch, lambda request: httpx.Response(403, json=body))
    with pytest.raises(GitHubError) as exc:
        asyncio.run(service.list_branches(USER, "octocat", "demo"))
    assert exc.value.code == "repo_not_found"

    limited = {"x-ratelimit-remaining": "0", "retry-after": "9"}
    service, _ = make_service(monkeypatch, lambda request: httpx.Response(403, json=body, headers=limited))
    with pytest.raises(GitHubError) as exc:
        asyncio.run(service.list_branches(USER, "octocat", "demo"))
    assert (exc.value.code, exc.value.extra) == ("github_rate_limited", {"retry_after": 9})

    service, _ = make_service(monkeypatch, lambda request: httpx.Response(422, json=body))
    with pytest.raises(GitHubError) as exc:
        asyncio.run(service.create_repo(USER, RepoCreateRequest(name="demo")))
    assert exc.value.status_code == 422
    assert exc.value.extra["errors"][0]["loc"] == ["body", "name"]


# --------------------------------------------------------------------------- #
# Connection problems and the error boundary
# --------------------------------------------------------------------------- #
def test_brief_connection_failures_are_retried(monkeypatch):
    attempts = []

    def flaky(request):
        attempts.append(1)
        if len(attempts) < 3:
            raise httpx.ConnectError("reset")
        return httpx.Response(200, json=[{"name": "main", "commit": {"sha": HEAD}}])

    service, _ = make_service(monkeypatch, flaky)
    assert asyncio.run(service.list_branches(USER, "octocat", "demo")).items[0].name == "main"
    assert len(attempts) == 3


def test_github_5xx_on_reads_is_retried(monkeypatch):
    attempts = []

    def flaky(request):
        attempts.append(1)
        return httpx.Response(503) if len(attempts) == 1 else httpx.Response(200, json=[])

    service, _ = make_service(monkeypatch, flaky)
    assert asyncio.run(service.list_branches(USER, "octocat", "demo")).items == []
    assert len(attempts) == 2


def test_a_write_that_may_have_reached_github_is_not_repeated(monkeypatch):
    posts = []
    fake = FakeGitHub()

    def handler(request):
        if request.method == "POST":
            posts.append(1)
            raise httpx.ReadTimeout("slow")
        return fake(request)

    service, _ = make_service(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        asyncio.run(service.commit(USER, "octocat", "demo", commit_request()))
    assert exc.value.code == "github_unavailable"
    assert posts == [1]


def test_unexpected_errors_become_a_clean_500(monkeypatch):
    service, _ = make_service(monkeypatch, FakeGitHub())

    async def broken(user_id):
        raise RuntimeError("internal detail that must not leak")

    service._github.get_connection = broken
    with pytest.raises(GitHubError) as exc:
        asyncio.run(service.get_connection(USER))
    assert exc.value.status_code == 500
    assert "internal detail" not in exc.value.message


def test_misconfigured_encryption_key_never_wipes_connections(monkeypatch):
    service, store = make_service(monkeypatch, FakeGitHub())
    service._settings.github_token_encryption_key = "not-a-fernet-key"
    with pytest.raises(GitHubError) as exc:
        asyncio.run(service.list_branches(USER, "octocat", "demo"))
    assert exc.value.code == "github_unavailable"
    assert store.connection is not None and not store.deleted


# --------------------------------------------------------------------------- #
# Second review round
# --------------------------------------------------------------------------- #
def test_build_like_folder_names_are_only_ignored_at_the_root():
    entries = [
        blob("build/out.js"),
        blob("src/main/java/com/acme/build/Builder.java"),
        blob("app/out/report.py"),
        blob("lib/node_modules/x/index.js"),
    ]
    selected, skipped = plan_snapshot(entries)
    assert [e["path"] for e in selected] == ["app/out/report.py", "src/main/java/com/acme/build/Builder.java"]
    assert {s.path: s.reason for s in skipped} == {"build/": "ignored_dir", "lib/node_modules/": "ignored_dir"}


@pytest.mark.parametrize("shared, expected", [(False, "grant"), (True, "token")])
def test_disconnect_never_breaks_another_users_link(monkeypatch, shared, expected):
    service, store = make_service(monkeypatch, FakeGitHub())
    store.shared = shared
    asyncio.run(service.disconnect(USER))
    assert store.connection is None
    assert (store.revoked, store.revoked_tokens) == ((["gho_token"], []) if expected == "grant" else ([], ["gho_token"]))


def test_get_connection_always_reads_fresh_rows():
    from sqlalchemy.dialects import postgresql

    from database.github_persistence import GitHubPersistence

    captured = []

    class Result:
        def scalar_one_or_none(self):
            return None

    class Session:
        async def execute(self, statement):
            captured.append(statement)
            return Result()

    asyncio.run(GitHubPersistence(Session()).get_connection(USER.id))
    assert captured[0].get_execution_options().get("populate_existing") is True
    assert "github_connections.user_id" in str(captured[0].compile(dialect=postgresql.dialect()))


def test_undecryptable_connection_reads_as_disconnected(monkeypatch):
    service, store = make_service(monkeypatch, FakeGitHub())
    rotated = GitHubService(session=None, settings=settings(jwt_secret_key="rotated"))
    rotated._github = store
    view = asyncio.run(rotated.get_connection(USER))
    assert view.connected is False
    assert not store.deleted  # a GET never deletes
    with pytest.raises(GitHubError) as exc:
        asyncio.run(rotated.list_branches(USER, "octocat", "demo"))
    assert exc.value.code == "github_reauth_required"
    assert store.deleted  # the next real GitHub call cleans it up


def test_upsert_takes_the_user_lock_and_returns_the_replaced_token():
    from sqlalchemy.dialects import postgresql

    from database.github_persistence import GitHubPersistence

    statements = []

    class Result:
        def scalar_one_or_none(self):
            # 2nd statement: the token being replaced; 4th: get_connection.
            return "old-encrypted" if len(statements) == 2 else "row"

    class Nested:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

    class Session:
        def begin_nested(self):
            return Nested()

        async def execute(self, statement):
            statements.append(str(statement.compile(dialect=postgresql.dialect())))
            return Result()

        async def commit(self):
            pass

    connection, previous = asyncio.run(
        GitHubPersistence(Session()).upsert_connection(
            USER.id, github_user_id=7, login="octocat", avatar_url=None, scopes=["repo"], access_token_encrypted="new"
        )
    )
    assert (connection, previous) == ("row", "old-encrypted")
    assert "pg_advisory_xact_lock" in statements[0]
    assert "access_token_encrypted" in statements[1] and "INSERT" in statements[2]


def test_parallel_connects_revoke_the_token_actually_replaced(monkeypatch):
    # Flow A stored gho_a while flow B was still exchanging its code: B must
    # revoke gho_a (what its upsert replaced), not the stale gho_token.
    service, store = make_service(monkeypatch, lambda request: httpx.Response(200, json={"id": 7, "login": "octocat"}))

    async def fake_exchange(*args):
        return {"access_token": "gho_b", "scope": "repo"}

    async def fake_upsert(user_id, **values):
        replaced = service._fernet().encrypt(b"gho_a").decode()
        store.connection = SimpleNamespace(**values, connected_at=datetime.now(timezone.utc))
        return store.connection, replaced

    monkeypatch.setattr(github_service_module, "exchange_oauth_code", fake_exchange)
    store.upsert_connection = fake_upsert
    state = httpx.URL(asyncio.run(service.start_oauth(USER, "/codelab/playground")).authorize_url).params["state"]
    asyncio.run(service.complete_oauth(USER, "code", state))
    assert store.revoked_tokens == ["gho_a"]


def test_write_without_permission_says_so(monkeypatch):
    fake = FakeGitHub()

    def handler(request):
        if request.method == "POST":
            return httpx.Response(403, json={"message": "Resource not accessible by integration"})
        return fake(request)

    service, _ = make_service(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        asyncio.run(service.commit(USER, "octocat", "demo", commit_request()))
    assert exc.value.code == "repo_not_found"
    assert "permission" in exc.value.message


def test_binary_files_give_their_budget_back(monkeypatch):
    # Extensionless binaries sort first and would fill the file budget alone.
    monkeypatch.setattr(github_service_module, "MAX_FILES", 3)
    entries = [blob("a0"), blob("a1"), blob("a2"), blob("z.py")]
    payload = {entries[i]["sha"]: b"\x00\x01" for i in range(3)}
    payload[entries[3]["sha"]] = b"print(1)\n"

    def handler(request):
        base = "/repos/octocat/demo"
        path = request.url.path
        if path == f"{base}/branches/main":
            return httpx.Response(200, json={"commit": {"sha": HEAD}})
        if path == f"{base}/git/trees/{HEAD}":
            return httpx.Response(200, json={"tree": entries, "truncated": False})
        return httpx.Response(200, json={"content": base64.b64encode(payload[path.rsplit("/", 1)[-1]]).decode()})

    service, _ = make_service(monkeypatch, handler)
    snap = asyncio.run(service.snapshot(USER, "octocat", "demo", "main", "auto"))
    assert [f.path for f in snap.files] == ["z.py"]
    assert {s.path: s.reason for s in snap.skipped} == {"a0": "binary", "a1": "binary", "a2": "binary"}


def test_search_reports_more_when_the_scan_limit_was_hit(monkeypatch):
    next_link = {"link": '<https://api.github.com/user/repos?page=99>; rel="next"'}

    def handler(request):
        page = int(request.url.params["page"])
        repos = [
            {"owner": {"login": "o"}, "name": f"other-{page}-{i}", "full_name": f"o/other-{page}-{i}", "private": False,
             "default_branch": "main", "html_url": "https://github.com/o/x", "updated_at": None}
            for i in range(100)
        ]
        return httpx.Response(200, json=repos, headers=next_link)

    service, _ = make_service(monkeypatch, handler)
    result = asyncio.run(service.list_repos(USER, 1, 30, "needle"))
    assert result.items == [] and result.has_more is True


def test_repo_created_but_not_ready_says_not_to_create_again(monkeypatch):
    monkeypatch.setattr(github_service_module, "REPO_READY_DELAYS", (0, 0))
    created = {"owner": {"login": "octocat"}, "name": "demo", "full_name": "octocat/demo", "private": True,
               "default_branch": "main", "html_url": "https://github.com/octocat/demo"}

    def handler(request):
        if request.method == "POST":
            return httpx.Response(201, json=created)
        return httpx.Response(404, json={"message": "Branch not found"})

    service, _ = make_service(monkeypatch, handler)
    with pytest.raises(GitHubError) as exc:
        asyncio.run(service.create_repo(USER, RepoCreateRequest(name="demo")))
    assert exc.value.code == "github_unavailable"
    assert "octocat/demo was created" in exc.value.message


def _limit_app():
    from core.request_limits import GitHubBodyLimitMiddleware

    app = FastAPI()

    @app.post("/api/github/echo")
    async def echo(payload: dict):
        return payload

    app.add_middleware(GitHubBodyLimitMiddleware, max_bytes=100)
    return TestClient(app)


def test_body_limit_accepts_small_chunked_bodies_and_rejects_large_ones():
    client = _limit_app()

    def chunks(data: bytes):
        yield data[:5]
        yield data[5:]

    small = b'{"a": 1}'
    response = client.post("/api/github/echo", content=chunks(small), headers={"content-type": "application/json"})
    assert response.status_code == 200 and response.json() == {"a": 1}

    big = b'{"a": "' + b"x" * 200 + b'"}'
    response = client.post("/api/github/echo", content=chunks(big), headers={"content-type": "application/json"})
    assert response.status_code == 413 and response.json()["code"] == "payload_too_large"

    response = client.post("/api/github/echo", content=small, headers={"content-type": "application/json"})
    assert response.status_code == 200


# --------------------------------------------------------------------------- #
# Full-review hardening
# --------------------------------------------------------------------------- #
def test_lone_surrogates_are_a_422_not_a_crash(monkeypatch):
    service, _ = make_service(monkeypatch, FakeGitHub())
    client = http_client(service)
    body = (
        '{"branch": "main", "base_commit_sha": "' + HEAD + '", "message": "m", '
        '"changes": [{"path": "a.py", "action": "upsert", "content": "\\ud800"}]}'
    )
    response = client.post(
        "/api/github/repos/octocat/demo/commits", content=body.encode(), headers={"content-type": "application/json"}
    )
    assert response.status_code == 422
    assert response.json()["errors"][0]["loc"][0] == "body"
    # Models built in code (not from JSON) are rejected too - by pydantic's own
    # string check (string_unicode) or, failing that, by our validator.
    with pytest.raises(PydanticValidationError, match="valid UTF-8|unicode"):
        commit_request(changes=[{"path": "a.py", "action": "upsert", "content": "\ud800"}])
    with pytest.raises(PydanticValidationError, match="valid UTF-8|unicode"):
        commit_request(message="bad \ud800")


def test_return_to_rejects_percent_encoded_traversal():
    for value in ["/codelab/%2e%2e/admin", "/codelab/%2E%2E%2Fadmin", "/codelab/%5cevil", "/codelab/a%2f%2fevil.example"]:
        with pytest.raises(PydanticValidationError):
            OAuthStartRequest(return_to=value)
    OAuthStartRequest(return_to="/codelab/playground?file=a%20b")


def test_one_malformed_repo_does_not_hide_the_others(monkeypatch):
    good = {"owner": {"login": "o"}, "name": "ok", "full_name": "o/ok", "private": False,
            "default_branch": "main", "html_url": "https://github.com/o/ok", "updated_at": None}
    service, _ = make_service(monkeypatch, lambda request: httpx.Response(200, json=[{"broken": True}, good, None]))
    assert [r.name for r in asyncio.run(service.list_repos(USER, 1, 30, None)).items] == ["ok"]


def test_slow_github_hits_the_deadline_with_a_clear_message(monkeypatch):
    monkeypatch.setattr(github_service_module, "OPERATION_DEADLINE_SECONDS", 0.05)

    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json=[])

    service, _ = make_service(monkeypatch, slow)
    with pytest.raises(GitHubError) as exc:
        asyncio.run(service.list_branches(USER, "octocat", "demo"))
    assert exc.value.code == "github_unavailable"
    assert "too slowly" in exc.value.message


def test_token_is_revoked_when_it_cannot_be_stored(monkeypatch):
    from core.exceptions import DatabaseError

    service, store = make_service(monkeypatch, lambda request: httpx.Response(200, json={"id": 7, "login": "octocat"}), connected=False)

    async def fake_exchange(*args):
        return {"access_token": "gho_new", "scope": "repo"}

    async def failing_upsert(user_id, **values):
        raise DatabaseError("Could not save data to the database right now. Please try again shortly.")

    monkeypatch.setattr(github_service_module, "exchange_oauth_code", fake_exchange)
    store.upsert_connection = failing_upsert
    state = httpx.URL(asyncio.run(service.start_oauth(USER, "/codelab/playground")).authorize_url).params["state"]
    with pytest.raises(DatabaseError):
        asyncio.run(service.complete_oauth(USER, "code", state))
    assert store.revoked_tokens == ["gho_new"]
