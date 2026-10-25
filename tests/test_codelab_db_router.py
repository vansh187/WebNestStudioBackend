"""CodeLab database proxy: QueryService and the HTTP contract through the real
app, with fake connectors and DNS. Every error code, the order of checks,
concurrency and timeouts, login, CORS, and that no URL, password or SQL text
reaches a log line. No network and no database."""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("SUPABASE_URL", "postgresql://user:pass@localhost:5432/test")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

import pytest
from fastapi.testclient import TestClient

from app import app
from codelab_db import dependencies as db_dependencies
from codelab_db import errors
from codelab_db.dependencies import check_db_enabled, get_body_reader, get_db_user, get_query_service
from codelab_db.infra.json_host_allowlist import load_host_allowlist
from codelab_db.infra.memory_limiter import MemoryLimiter
from codelab_db.protocols import DbUser, MongoResult, ServerInfo, SqlResult
from codelab_db.services import query_service as query_service_module
from codelab_db.services.http_io import BodyReader
from codelab_db.services.query_service import QueryService
from codelab_db.services.statement_guard import MongoGuard, MySqlGuard, PostgresGuard
from codelab_db.services.target_validator import TargetValidator
from codelab_db.settings import DbProxySettings
from core.dependencies import container, get_auth_service, get_db_session
from core.exceptions import UnauthorizedError

HOSTS_FILE = Path(__file__).resolve().parent.parent / "codelab_db" / "hosts.json"
USER = DbUser(uuid.uuid4())
ORIGIN = "https://www.webneststudio.co.in"
PASSWORD = "s3cr3t-Pa55word"
PG_URL = f"postgresql://postgres.abcd:{PASSWORD}@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"
MONGO_URL = f"mongodb+srv://learner:{PASSWORD}@cluster0.ab12c.mongodb.net/shop"
SECRET_SQL = "SELECT id, title FROM secret_table_name WHERE done = %s"


class FakeClock:
    def now(self):
        return datetime(2026, 10, 25, 12, tzinfo=timezone.utc)


class FakeResolver:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    async def resolve_public(self, host):
        self.calls.append(host)
        if self.error:
            raise self.error
        return ["3.6.1.1"]

    async def resolve_srv(self, name):
        self.calls.append(name)
        return [("ac-1-shard-00-00.ab12c.mongodb.net", 27017)]

    async def resolve_txt(self, name):
        return []


class FakeCapacity:
    def __init__(self, room=True):
        self.room = room

    def has_room(self):
        return self.room


class FakeConnector:
    def __init__(self, result=None, error=None, delay=0.0):
        self.result = result or SqlResult(["id", "title"], [[1, "Learn FastAPI"]], 1)
        self.error = error
        self.delay = delay
        self.calls = []

    async def run(self, target, request, limits):
        self.calls.append((target, request, limits))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.result

    async def test(self, target, limits):
        self.calls.append((target, None, limits))
        if self.error:
            raise self.error
        return ServerInfo("16.4")


class World:
    def __init__(self, connectors=None, resolver=None, capacity=None, **settings):
        self.settings = DbProxySettings(**{"enabled": True, **settings})
        self.resolver = resolver or FakeResolver()
        self.capacity = capacity or FakeCapacity()
        self.connectors = connectors if connectors is not None else {
            "postgres": FakeConnector(), "mysql": FakeConnector(),
            "mongodb": FakeConnector(MongoResult([{"_id": {"$oid": "6719f0c2e4b0a1b2c3d4e5f6"}, "status": "paid"}],
                                                 1, None))}
        self.limiter = MemoryLimiter(self.settings.per_minute, self.settings.per_day)
        self.service = QueryService(
            self.settings, TargetValidator(load_host_allowlist(str(HOSTS_FILE)), self.resolver),
            {"postgres": PostgresGuard(self.settings.max_sql_bytes), "mysql": MySqlGuard(self.settings.max_sql_bytes),
             "mongodb": MongoGuard()}, self.connectors, self.limiter, FakeClock(), self.capacity)


@pytest.fixture
def world():
    return World()


@pytest.fixture
def client(world):
    def install(target: World):
        app.dependency_overrides[get_query_service] = lambda: target.service

    app.dependency_overrides[check_db_enabled] = lambda: None
    app.dependency_overrides[get_db_user] = lambda: USER
    install(world)
    test_client = TestClient(app)
    test_client.install = install
    yield test_client
    app.dependency_overrides.clear()


def sql_body(**fields):
    return {"engine": "postgres", "url": PG_URL, "sql": SECRET_SQL, "params": [False], **fields}


def mongo_body(**fields):
    return {"engine": "mongodb", "url": MONGO_URL, "operation": "find", "collection": "orders",
            "filter": {"status": "paid"}, "options": {"limit": 20}, **fields}


def assert_db_error(response, status, code, /, **extra):
    assert response.status_code == status, response.text
    assert response.headers["x-webnest-db-error"] == code
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    body = response.json()
    assert body["code"] == code and body["detail"]
    for key, value in extra.items():
        assert body[key] == value, (key, body)
    assert PASSWORD not in response.text
    return body


# ---- success shapes ----------------------------------------------------------------------


def test_sql_query_success(client, world):
    response = client.post("/api/codelab/db/query", json=sql_body())
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["columns"] == ["id", "title"] and body["rows"] == [[1, "Learn FastAPI"]]
    assert body["row_count"] == 1 and body["truncated"] is False and isinstance(body["ms"], int)
    target, statement, limits = world.connectors["postgres"].calls[0]
    assert (target.host, target.address, target.port, target.tls_verify) == (
        "aws-0-ap-south-1.pooler.supabase.com", "3.6.1.1", 5432, False)
    assert (statement.kind, statement.params) == ("cursor", [False])
    assert (limits.statement_timeout_seconds, limits.max_rows, limits.max_bytes) == (10, 500, 2097152)
    assert "x-webnest-db-error" not in response.headers


def test_sql_write_shape(client, world):
    world.connectors["mysql"].result = SqlResult([], [], 3)
    response = client.post("/api/codelab/db/query", json={"engine": "mysql", "sql": "UPDATE t SET a = 1",
                                                          "url": f"mysql://u:{PASSWORD}@mysql-a.aivencloud.com/db"})
    assert response.json()["columns"] == [] and response.json()["row_count"] == 3


def test_mongo_find_and_write_shapes(client, world):
    response = client.post("/api/codelab/db/query", json=mongo_body())
    body = response.json()
    assert response.status_code == 200 and body["documents"][0]["_id"] == {"$oid": "6719f0c2e4b0a1b2c3d4e5f6"}
    assert body["count"] == 1 and body["truncated"] is False and "write" not in body
    operation = world.connectors["mongodb"].calls[0][1]
    assert (operation.database, operation.collection, operation.limit) == ("shop", "orders", 20)
    world.connectors["mongodb"].result = MongoResult([], 2, {"inserted_ids": [{"$oid": "a" * 24}, {"$oid": "b" * 24}]})
    write = client.post("/api/codelab/db/query", json=mongo_body(operation="insert_many", documents=[{}, {}])).json()
    assert write["count"] == 2 and write["write"]["inserted_ids"][1] == {"$oid": "b" * 24}


def test_connection_test_endpoint(client):
    response = client.post("/api/codelab/db/test", json={"engine": "postgres", "url": PG_URL})
    assert response.status_code == 200 and response.json()["ok"] is True
    assert response.json()["server_version"] == "16.4"


def test_status_is_public_and_describes_the_proxy():
    app.dependency_overrides.clear()
    response = TestClient(app).get("/api/codelab/db/status")
    assert response.status_code == 200 and response.headers["cache-control"] == "public, max-age=300"
    body = response.json()
    assert body["enabled"] is False   # off by default
    assert set(body["engines"]) == {"postgres", "mysql", "mongodb"}
    assert body["engines"]["postgres"]["available"] is True
    assert body["limits"]["max_rows"] == 500


# ---- every error code ---------------------------------------------------------------------


@pytest.mark.parametrize("payload,status,code", [
    (b"not json", 400, "db_bad_request"), (b"[]", 400, "db_bad_request"),
    (json.dumps({"engine": "redis", "url": "x"}).encode(), 400, "db_bad_request"),
    (json.dumps(sql_body(url="mysql://u:p@mysql-a.aivencloud.com/db")).encode(), 400, "db_bad_url"),
    (json.dumps(sql_body(url=PG_URL + "?sslmode=disable")).encode(), 400, "db_bad_url"),
    (json.dumps(sql_body(url=PG_URL.replace("aws-0-ap-south-1.pooler.supabase.com", "10.0.0.1"))).encode(),
     403, "db_host_not_allowed"),
    (json.dumps(sql_body(url=PG_URL.replace("aws-0-ap-south-1.pooler.supabase.com", "internal.corp.local"))).encode(),
     403, "db_host_not_allowed"),
    (json.dumps(sql_body(sql="COPY t TO PROGRAM 'x'")).encode(), 403, "db_statement_blocked"),
    (json.dumps(sql_body(sql="SELECT 1; SELECT 2")).encode(), 403, "db_statement_blocked"),
    (json.dumps(mongo_body(database="admin")).encode(), 403, "db_statement_blocked"),
    (json.dumps(mongo_body(filter={"$where": "1"})).encode(), 403, "db_statement_blocked"),
])
def test_refusals_before_any_network_call(client, world, payload, status, code):
    response = client.post("/api/codelab/db/query", content=payload, headers={"Content-Type": "application/json"})
    assert_db_error(response, status, code)
    assert world.resolver.calls == [] and all(not c.calls for c in world.connectors.values())
    assert world.limiter._day_counts == {}   # refused requests don't use the quota


@pytest.mark.parametrize("error,status,code", [
    (errors.QueryTimeout(10), 408, "db_timeout"), (errors.ConnectFailed("password authentication failed"), 502,
                                                   "db_connect_failed"),
    (errors.QueryError('relation "tasks" does not exist'), 422, "db_query_error"),
])
def test_database_errors(client, error, status, code):
    client.install(World(connectors={"postgres": FakeConnector(error=error)}))
    assert_db_error(client.post("/api/codelab/db/query", json=sql_body()), status, code)


def test_result_too_large_returns_the_part_that_fit(client):
    partial = {"columns": ["id"], "rows": [[1], [2]], "row_count": 2}
    client.install(World(connectors={"postgres": FakeConnector(error=errors.ResultTooLarge(2, 1000, partial))}))
    body = assert_db_error(client.post("/api/codelab/db/query", json=sql_body()), 413, "db_result_too_large",
                           truncated=True, rows=[[1], [2]], columns=["id"], max_rows=2)
    assert isinstance(body["ms"], int)


def test_dns_refusal_and_failure(client):
    client.install(World(resolver=FakeResolver(errors.HostNotAllowed("aws-0-x.pooler.supabase.com",
                                                                      "resolves to a private or reserved address"))))
    assert_db_error(client.post("/api/codelab/db/query", json=sql_body()), 403, "db_host_not_allowed")
    client.install(World(resolver=FakeResolver(errors.ConnectFailed("the host could not be found"))))
    assert_db_error(client.post("/api/codelab/db/query", json=sql_body()), 502, "db_connect_failed")


def test_rate_limit(client):
    client.install(World(per_minute=1))
    assert client.post("/api/codelab/db/query", json=sql_body()).status_code == 200
    response = client.post("/api/codelab/db/query", json=sql_body())
    body = assert_db_error(response, 429, "db_rate_limited", scope="minute")
    assert response.headers["retry-after"] == str(body["retry_after"])


def test_blocked_user_and_unavailable_engine(client):
    client.install(World(blocked_user_ids_text=str(USER.id)))
    assert_db_error(client.post("/api/codelab/db/query", json=sql_body()), 403, "db_user_blocked")
    client.install(World(connectors={"postgres": FakeConnector()}))
    assert_db_error(client.post("/api/codelab/db/query", json=mongo_body()), 503, "db_disabled")


def test_body_size_cap(client):
    app.dependency_overrides[get_body_reader] = lambda: BodyReader(1024)
    assert_db_error(client.post("/api/codelab/db/query", json=sql_body(sql="SELECT '" + "x" * 2000 + "'")), 413,
                    "db_payload_too_large", limit_bytes=1024)

    def chunks():
        yield b'{"engine": "postgres", "sql": "' + b"x" * 600
        yield b"x" * 600 + b'"}'

    assert_db_error(client.post("/api/codelab/db/query", content=chunks()), 413, "db_payload_too_large")
    assert_db_error(client.post("/api/codelab/db/query", content=b""), 400, "db_bad_request")


def test_switched_off_proxy_answers_503_before_login():
    app.dependency_overrides.clear()
    client = TestClient(app)
    for path, payload in (("/api/codelab/db/query", sql_body()), ("/api/codelab/db/test",
                                                                  {"engine": "postgres", "url": PG_URL})):
        assert_db_error(client.post(path, json=payload), 503, "db_disabled")


def test_service_refuses_when_switched_off_even_if_the_router_check_is_bypassed(client):
    client.install(World(enabled=False))
    assert_db_error(client.post("/api/codelab/db/query", json=sql_body()), 503, "db_disabled")


# ---- concurrency and the overall time limit -------------------------------------------------


def test_concurrency_cap(world):
    busy = World(max_concurrent=1, connectors={"postgres": FakeConnector(delay=0.2)})
    request = SimpleNamespace(engine="postgres", url=SimpleNamespace(get_secret_value=lambda: PG_URL),
                              sql="SELECT 1", params=None)

    async def both():
        return await asyncio.gather(busy.service.query(USER.id, request), busy.service.query(USER.id, request),
                                    return_exceptions=True)

    first, second = asyncio.run(both())
    assert isinstance(first, dict) and isinstance(second, errors.Busy)
    assert second.extra == {"retry_after": 2, "scope": "busy"} and busy.service._running == 0


def test_busy_worker_threads_refuse_fast_without_using_quota(client):
    full = World(capacity=FakeCapacity(room=False))
    client.install(full)
    body = assert_db_error(client.post("/api/codelab/db/query", json=sql_body()), 429, "db_rate_limited",
                           scope="busy")
    assert body["retry_after"] == 2
    assert full.resolver.calls == [] and full.limiter._day_counts == {}


def test_refusals_before_the_database_do_not_use_quota(client):
    for resolver in (FakeResolver(errors.HostNotAllowed("x", "resolves to a private or reserved address")),
                     FakeResolver(errors.ConnectFailed("looking up the database host timed out"))):
        world = World(resolver=resolver, per_minute=1)
        client.install(world)
        for _ in range(3):   # never rate-limited: nothing reached the database
            assert client.post("/api/codelab/db/query", json=sql_body()).status_code in (403, 502)
        assert world.limiter._day_counts == {}
    counted = World(per_minute=1)
    client.install(counted)
    assert client.post("/api/codelab/db/query", json=sql_body()).status_code == 200
    assert counted.limiter._day_counts == {USER.id: 1}


def test_overall_time_limit(monkeypatch):
    monkeypatch.setattr(query_service_module, "OVERALL_SLACK_SECONDS", -6.9)   # 2 + 5 - 6.9 = 0.1 s
    slow = World(statement_timeout_seconds=5, connect_timeout_seconds=2,
                 connectors={"postgres": FakeConnector(delay=2)})
    request = SimpleNamespace(engine="postgres", url=SimpleNamespace(get_secret_value=lambda: PG_URL),
                              sql="SELECT 1", params=None)
    with pytest.raises(errors.QueryTimeout):
        asyncio.run(slow.service.query(USER.id, request))
    assert slow.service._running == 0


def test_unexpected_connector_errors_become_query_errors(client):
    client.install(World(connectors={"postgres": FakeConnector(error=RuntimeError(f"boom {PASSWORD}"))}))
    body = assert_db_error(client.post("/api/codelab/db/query", json=sql_body()), 422, "db_query_error")
    assert "boom" not in body["detail"]


# ---- logs ---------------------------------------------------------------------------------


def test_logs_never_contain_the_url_password_or_sql(client, caplog):
    with caplog.at_level(logging.DEBUG):
        client.post("/api/codelab/db/query", json=sql_body())
        client.post("/api/codelab/db/query", json=sql_body(sql="COPY t TO PROGRAM 'x'"))
        client.post("/api/codelab/db/query", json=mongo_body())
        client.post("/api/codelab/db/query", json=sql_body(url=PG_URL + "?sslmode=disable"))
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert PASSWORD not in text and "secret_table_name" not in text and "postgres.abcd" not in text
    assert "pooler.supabase.com" in text and "type=SELECT" in text and "status=ok" in text
    assert "aws-0-ap-south-1" not in text   # domain only


# ---- login ----------------------------------------------------------------------------------


class FakeAuth:
    def __init__(self, user=None):
        self.user = user
        self.tokens = []

    async def get_current_user(self, token):
        self.tokens.append(token)
        if self.user is None:
            raise UnauthorizedError("Invalid or expired access token")
        return self.user


class FakeSession:
    def __init__(self):
        self.closed = 0

    async def close(self):
        self.closed += 1


@pytest.fixture
def login(client):
    app.dependency_overrides.pop(get_db_user)
    auth = FakeAuth()
    auth.session = FakeSession()
    app.dependency_overrides[get_auth_service] = lambda: auth
    app.dependency_overrides[get_db_session] = lambda: auth.session
    return auth


@pytest.mark.parametrize("headers", [{}, {"X-WebNest-Authorization": "Bearer"}, {"X-WebNest-Authorization": "Token x"},
                                     {"X-WebNest-Authorization": "Bearer expired"},
                                     {"Authorization": "Bearer webnest-token"}])
def test_login_comes_from_the_webnest_header_only(client, login, headers):
    assert_db_error(client.post("/api/codelab/db/query", json=sql_body(), headers=headers), 401, "db_login_required")


def test_login_rules(client, login, monkeypatch):
    login.user = SimpleNamespace(id=uuid.uuid4(), is_active=True, is_verified=False)
    headers = {"X-WebNest-Authorization": "Bearer ok"}
    monkeypatch.setattr(container.settings, "email_enabled", False)
    assert client.post("/api/codelab/db/query", json=sql_body(), headers=headers).status_code == 200
    assert login.session.closed == 1   # handed back before the database call
    monkeypatch.setattr(container.settings, "email_enabled", True)
    assert_db_error(client.post("/api/codelab/db/query", json=sql_body(), headers=headers), 401, "db_login_required")
    login.user = SimpleNamespace(id=uuid.uuid4(), is_active=False, is_verified=True)
    assert_db_error(client.post("/api/codelab/db/query", json=sql_body(), headers=headers), 401, "db_login_required")


# ---- CORS -------------------------------------------------------------------------------------


def preflight(origin=ORIGIN, headers="x-webnest-authorization,content-type", path="/api/codelab/db/query"):
    return TestClient(app).options(path, headers={"Origin": origin, "Access-Control-Request-Method": "POST",
                                                  "Access-Control-Request-Headers": headers})


def test_db_preflight_is_strict():
    response = preflight()
    assert response.status_code == 200 and response.headers["access-control-allow-origin"] == ORIGIN
    assert "access-control-allow-credentials" not in response.headers
    assert preflight(origin="https://evil.example").status_code == 400
    assert preflight(headers="x-webnest-authorization,authorization").status_code == 400


def test_db_responses_expose_the_error_header(client):
    response = client.post("/api/codelab/db/query", json=sql_body(sql="COPY t TO PROGRAM 'x'"),
                           headers={"Origin": ORIGIN})
    exposed = {h.strip().lower() for h in response.headers["access-control-expose-headers"].split(",")}
    assert {"x-webnest-db-error", "retry-after"} <= exposed


def test_other_routes_keep_their_cors_policies():
    relay = preflight(path="/api/codelab/relay/api.razorpay.com/v1/orders", headers="x-webnest-authorization")
    assert relay.status_code == 200 and "access-control-allow-credentials" not in relay.headers
    site = preflight(path="/api/github/status", headers="authorization")
    assert site.status_code == 200 and site.headers.get("access-control-allow-credentials") == "true"
    lookalike = preflight(path="/api/codelab/dbx", headers="authorization")
    assert lookalike.headers.get("access-control-allow-credentials") == "true"   # not the db policy
