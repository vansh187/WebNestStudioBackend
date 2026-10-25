"""CodeLab database proxy end to end: HTTP → router → QueryService → guards →
real connectors → real Postgres, MySQL/MariaDB and MongoDB servers with TLS.

The only stand-in is DNS: the throwaway servers run on this machine, so a
test allowlist (*.codelab.test) and resolver map those names to them; the
real allowlist and DNS checks have their own tests. Skipped unless:

  CODELAB_DB_PG_TEST, CODELAB_DB_MYSQL_TEST, CODELAB_DB_MONGO_TEST = host:port
  CODELAB_DB_TEST_CA_BUNDLE = CA bundle trusted for all three servers

Never point these at a production database."""

import asyncio
import dataclasses
import os
from types import SimpleNamespace

os.environ.setdefault("SUPABASE_URL", "postgresql://user:pass@localhost:5432/test")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

import pytest
from fastapi.testclient import TestClient

from app import app
from codelab_db.dependencies import check_db_enabled, get_db_user, get_query_service
from codelab_db.infra.json_host_allowlist import parse_allowlist
from codelab_db.infra.memory_limiter import MemoryLimiter
from codelab_db.infra.mongo_connector import MongoConnector
from codelab_db.infra.mysql_connector import MySqlConnector
from codelab_db.infra.pg_connector import PostgresConnector
from codelab_db.infra.renewable_executor import RenewableExecutor
from codelab_db.infra.system_clock import SystemClock
from codelab_db.protocols import DbUser
from codelab_db.schemas import parse_query_request
from codelab_db.services.query_service import QueryService
from codelab_db.services.statement_guard import MongoGuard, MySqlGuard, PostgresGuard
from codelab_db.services.target_validator import TargetValidator
from codelab_db.settings import DbProxySettings

PG = os.environ.get("CODELAB_DB_PG_TEST", "")
MY = os.environ.get("CODELAB_DB_MYSQL_TEST", "")
MONGO = os.environ.get("CODELAB_DB_MONGO_TEST", "")
CA_BUNDLE = os.environ.get("CODELAB_DB_TEST_CA_BUNDLE", "")
pytestmark = pytest.mark.skipif(not (PG and MY and MONGO and CA_BUNDLE), reason="test database servers not set")

USER = DbUser(__import__("uuid").uuid4())
PASSWORD = "Pr4ctice-Pass"


def port(server: str) -> int:
    return int(server.rpartition(":")[2]) if server else 0


class LocalResolver:
    """*.codelab.test → this machine."""

    def __init__(self):
        self.calls = []

    async def resolve_public(self, host):
        self.calls.append(host)
        return ["127.0.0.1"]

    async def resolve_srv(self, name):
        return []

    async def resolve_txt(self, name):
        return []


class LocalValidator(TargetValidator):
    """The real parse/resolve, then the certificate's name (localhost)."""

    async def resolve(self, parsed):
        target = await super().resolve(parsed)
        return dataclasses.replace(target, host="localhost", hosts=(("localhost", target.port),))


def build_service(**settings):
    allowlist = parse_allowlist({
        "postgres": {"default_port": port(PG), "ports": [port(PG)],
                     "hosts": [{"pattern": "*.codelab.test", "tls_verify": True, "label": "Local Postgres"}]},
        "mysql": {"default_port": port(MY), "ports": [port(MY)],
                  "hosts": [{"pattern": "*.codelab.test", "tls_verify": True, "label": "Local MariaDB"}]},
        "mongodb": {"default_port": port(MONGO), "ports": [port(MONGO)],
                    "hosts": [{"pattern": "*.codelab.test", "tls_verify": True, "label": "Local MongoDB"}]},
    })
    values = dict(enabled=True, ca_file=CA_BUNDLE, per_minute=10_000, per_day=100_000, statement_timeout_seconds=3,
                  max_rows=50)
    values.update(settings)
    config = DbProxySettings(**values)
    pool = RenewableExecutor(config.max_concurrent)
    return QueryService(config, LocalValidator(allowlist, LocalResolver()),
                        {"postgres": PostgresGuard(config.max_sql_bytes), "mysql": MySqlGuard(config.max_sql_bytes),
                         "mongodb": MongoGuard()},
                        {"postgres": PostgresConnector(pool), "mysql": MySqlConnector(pool),
                         "mongodb": MongoConnector(pool)},
                        MemoryLimiter(config.per_minute, config.per_day), SystemClock(), pool)


PG_URL = f"postgresql://postgres:{PASSWORD}@pg.codelab.test:{port(PG)}/postgres?sslmode=require"
MY_URL = f"mysql://root@mysql.codelab.test:{port(MY)}/codelab_e2e?ssl-mode=REQUIRED"
MY_URL_NO_DB = f"mysql://root@mysql.codelab.test:{port(MY)}"
MONGO_URL = f"mongodb://mongo.codelab.test:{port(MONGO)}/codelab_e2e?tls=true"


@pytest.fixture(scope="module")
def client():
    service = build_service()
    app.dependency_overrides[check_db_enabled] = lambda: None
    app.dependency_overrides[get_db_user] = lambda: USER
    app.dependency_overrides[get_query_service] = lambda: service
    test_client = TestClient(app)
    test_client.service = service
    yield test_client
    app.dependency_overrides.clear()


def query(client, **payload):
    return client.post("/api/codelab/db/query", json=payload)


def ok(response):
    assert response.status_code == 200, response.text
    assert PASSWORD not in response.text
    return response.json()


# ---- connection tests, all three engines -------------------------------------------------


def test_connection_test_succeeds_for_every_engine(client):
    for engine, url in (("postgres", PG_URL), ("mysql", MY_URL_NO_DB), ("mongodb", MONGO_URL)):
        body = ok(client.post("/api/codelab/db/test", json={"engine": engine, "url": url}))
        assert body["ok"] is True and body["server_version"], engine


# ---- Postgres --------------------------------------------------------------------------------


def test_postgres_through_the_api(client):
    ok(query(client, engine="postgres", url=PG_URL, sql="DROP TABLE IF EXISTS e2e_tasks"))
    ok(query(client, engine="postgres", url=PG_URL,
             sql="CREATE TABLE e2e_tasks (id serial PRIMARY KEY, title text NOT NULL, done boolean DEFAULT false)"))
    inserted = ok(query(client, engine="postgres", url=PG_URL,
                        sql="INSERT INTO e2e_tasks (title) VALUES (%s), (%s), (%s)",
                        params=["Learn FastAPI", "Learn SQL", "Ship it"]))
    assert inserted["row_count"] == 3 and inserted["columns"] == []
    rows = ok(query(client, engine="postgres", url=PG_URL,
                    sql="SELECT id, title FROM e2e_tasks WHERE done = %s ORDER BY id LIMIT 10", params=[False]))
    assert rows["columns"] == ["id", "title"] and rows["rows"][0] == [1, "Learn FastAPI"] and rows["row_count"] == 3
    updated = ok(query(client, engine="postgres", url=PG_URL,
                       sql="UPDATE e2e_tasks SET done = true WHERE id = %(id)s RETURNING id, done", params={"id": 2}))
    assert updated["rows"] == [[2, True]]
    assert ok(query(client, engine="postgres", url=PG_URL, sql="SELECT count(*) FROM e2e_tasks WHERE done"))[
        "rows"] == [[1]]


def test_postgres_problems_are_reported_not_crashed(client):
    cases = [
        ("SELEC 1", 422, "db_query_error"), ("SELECT * FROM missing_table", 422, "db_query_error"),
        ("SELECT g FROM generate_series(1, 100000) g", 413, "db_result_too_large"),
        ("SELECT pg_sleep(10)", 408, "db_timeout"), ("COPY e2e_tasks TO PROGRAM 'x'", 403, "db_statement_blocked"),
    ]
    for sql, status, code in cases:
        response = query(client, engine="postgres", url=PG_URL, sql=sql)
        assert (response.status_code, response.json()["code"]) == (status, code), sql
    # The server is still fine afterwards.
    assert ok(query(client, engine="postgres", url=PG_URL, sql="SELECT 1 AS alive"))["rows"] == [[1]]


# ---- MySQL / MariaDB ---------------------------------------------------------------------------


def test_mysql_through_the_api(client):
    ok(query(client, engine="mysql", url=MY_URL_NO_DB, sql="CREATE DATABASE IF NOT EXISTS codelab_e2e CHARACTER SET utf8mb4"))
    ok(query(client, engine="mysql", url=MY_URL, sql="DROP TABLE IF EXISTS orders"))
    ok(query(client, engine="mysql", url=MY_URL,
             sql="CREATE TABLE orders (id INT PRIMARY KEY AUTO_INCREMENT, amount DECIMAL(10,2), note VARCHAR(50))"))
    assert ok(query(client, engine="mysql", url=MY_URL, sql="INSERT INTO orders (amount, note) VALUES (%s, %s), (%s, %s)",
                    params=["500.00", "first ✓", "99.50", "second"]))["row_count"] == 2
    rows = ok(query(client, engine="mysql", url=MY_URL, sql="SELECT id, amount, note FROM orders ORDER BY id"))
    assert rows["rows"] == [[1, "500.00", "first ✓"], [2, "99.50", "second"]]
    assert ok(query(client, engine="mysql", url=MY_URL, sql="DELETE FROM orders WHERE id = %s", params=[2]))[
        "row_count"] == 1


def test_mysql_problems_are_reported_not_crashed(client):
    cases = [("SELEC 1", 422, "db_query_error"), ("SELECT seq FROM seq_1_to_100000", 413, "db_result_too_large"),
             ("SELECT SLEEP(10)", 408, "db_timeout"), ("SELECT LOAD_FILE('C:/Windows/win.ini')", 403,
                                                        "db_statement_blocked")]
    for sql, status, code in cases:
        response = query(client, engine="mysql", url=MY_URL, sql=sql)
        assert (response.status_code, response.json()["code"]) == (status, code), sql
    assert ok(query(client, engine="mysql", url=MY_URL, sql="SELECT 1"))["rows"] == [[1]]
    wrong = client.post("/api/codelab/db/test", json={"engine": "mysql",
                                                      "url": f"mysql://nobody:{PASSWORD}@mysql.codelab.test:{port(MY)}"})
    assert wrong.status_code == 502 and wrong.json()["code"] == "db_connect_failed" and PASSWORD not in wrong.text


# ---- MongoDB -------------------------------------------------------------------------------------


def test_mongodb_through_the_api(client):
    base = {"engine": "mongodb", "url": MONGO_URL, "collection": "orders"}
    ok(query(client, **base, operation="delete_many", filter={}))
    inserted = ok(query(client, **base, operation="insert_many", documents=[
        {"status": "paid", "amount": 500, "created_at": {"$date": "2026-10-25T09:30:00Z"}},
        {"status": "paid", "amount": 250}, {"status": "new", "amount": 99}]))
    assert inserted["count"] == 3 and len(inserted["write"]["inserted_ids"]) == 3
    found = ok(query(client, **base, operation="find", filter={"status": "paid"},
                     options={"limit": 20, "sort": [["amount", -1]], "projection": {"_id": 0}}))
    assert found["documents"][0] == {"status": "paid", "amount": 500, "created_at": {"$date": "2026-10-25T09:30:00Z"}}
    assert ok(query(client, **base, operation="count_documents", filter={"status": "paid"}))["count"] == 2
    totals = ok(query(client, **base, operation="aggregate",
                      pipeline=[{"$group": {"_id": "$status", "total": {"$sum": "$amount"}}}, {"$sort": {"_id": 1}}]))
    assert totals["documents"] == [{"_id": "new", "total": 99}, {"_id": "paid", "total": 750}]
    assert ok(query(client, **base, operation="update_many", filter={"status": "paid"},
                    update={"$set": {"status": "shipped"}}))["write"]["modified_count"] == 2
    assert ok(query(client, **base, operation="delete_one", filter={"status": "new"}))["write"] == {"deleted_count": 1}
    names = ok(query(client, engine="mongodb", url=MONGO_URL, operation="list_collection_names"))
    assert "orders" in names["documents"]
    blocked = query(client, **base, operation="find", filter={"$where": "sleep(100)"})
    assert blocked.status_code == 403 and blocked.json()["code"] == "db_statement_blocked"


# ---- load: parallel queries, nothing left open -------------------------------------------------------


def test_parallel_queries_all_succeed_and_every_connection_is_closed(client):
    service = client.service
    request = parse_query_request(__import__("json").dumps(
        {"engine": "postgres", "url": PG_URL, "sql": "SELECT pg_sleep(0.2), 1 AS n"}).encode())
    mongo_request = parse_query_request(__import__("json").dumps(
        {"engine": "mongodb", "url": MONGO_URL, "operation": "count_documents", "collection": "orders"}).encode())
    mysql_request = parse_query_request(__import__("json").dumps(
        {"engine": "mysql", "url": MY_URL, "sql": "SELECT SLEEP(0.2), 1"}).encode())

    async def burst():
        calls = [service.query(USER.id, r) for r in (request, mongo_request, mysql_request) for _ in range(2)]
        return await asyncio.gather(*calls, return_exceptions=True)

    results = asyncio.run(burst())
    assert all(isinstance(result, dict) for result in results), results
    assert service._running == 0
    still_open = ok(query(client, engine="postgres", url=PG_URL,
                          sql="SELECT count(*) FROM pg_stat_activity WHERE application_name = 'webnest-codelab' "
                              "AND pid <> pg_backend_pid()"))
    assert still_open["rows"] == [[0]]
