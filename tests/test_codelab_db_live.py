"""CodeLab database proxy against real MySQL/MariaDB and MongoDB servers with
TLS. Skipped unless throwaway servers are given:

  CODELAB_DB_MYSQL_TEST=host:port   (root user, no password, TLS on, require_secure_transport)
  CODELAB_DB_MONGO_TEST=host:port   (TLS required, no auth)
  CODELAB_DB_TEST_CA=<CA / self-signed certificate of both servers>

Never point these at a production database."""

import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault("SUPABASE_URL", "postgresql://user:pass@localhost:5432/test")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

import pytest

from codelab_db import errors
from codelab_db.infra.mongo_connector import MongoConnector
from codelab_db.infra.mysql_connector import MySqlConnector
from codelab_db.infra.renewable_executor import RenewableExecutor
from codelab_db.protocols import Limits, SqlStatement, Target
from codelab_db.schemas import MongoQueryRequest
from codelab_db.services.statement_guard import MongoGuard, MySqlGuard

MYSQL = os.environ.get("CODELAB_DB_MYSQL_TEST", "")
MONGO = os.environ.get("CODELAB_DB_MONGO_TEST", "")
CA = os.environ.get("CODELAB_DB_TEST_CA", "")
POOL = RenewableExecutor(4)

needs_mysql = pytest.mark.skipif(not (MYSQL and CA), reason="CODELAB_DB_MYSQL_TEST / CODELAB_DB_TEST_CA not set")
needs_mongo = pytest.mark.skipif(not (MONGO and CA), reason="CODELAB_DB_MONGO_TEST / CODELAB_DB_TEST_CA not set")


def run(coroutine):
    return asyncio.run(coroutine)


def limits(timeout=3, rows=3, max_bytes=100_000, ca=None):
    return Limits(timeout, 5, rows, max_bytes, CA if ca is None else (ca or None))


# ---- MySQL / MariaDB ---------------------------------------------------------------------


def mysql_target(user="root", password="", database="codelab_test", verify=True):
    host, _, port = MYSQL.partition(":")
    return Target("mysql", "localhost", int(port), host, user, password, database, verify, False, (), {}, "localhost")


def my(sql, params=None, target=None, lim=None, guard=True):
    statement = (MySqlGuard(20480).check(SimpleNamespace(sql=sql, params=params), None) if guard
                 else SqlStatement(sql, params, "execute", "RAW"))
    return run(MySqlConnector(POOL).run(target or mysql_target(), statement, lim or limits()))


@pytest.fixture(scope="module")
def mysql_db():
    if not (MYSQL and CA):
        pytest.skip("no MySQL test server")
    my("CREATE DATABASE IF NOT EXISTS codelab_test CHARACTER SET utf8mb4", target=mysql_target(database=None))
    my("ALTER DATABASE codelab_test CHARACTER SET utf8mb4", target=mysql_target(database=None))
    my("DROP TABLE IF EXISTS tasks")
    my("CREATE TABLE tasks (id INT PRIMARY KEY AUTO_INCREMENT, title VARCHAR(100), price DECIMAL(10,2), "
       "due DATE, at DATETIME, blob_col BLOB, meta JSON, done TINYINT(1) DEFAULT 0)")
    return True


@needs_mysql
def test_mysql_round_trip(mysql_db):
    assert my("INSERT INTO tasks (title, price, due, at, blob_col, meta) VALUES "
              "(%s, %s, %s, %s, %s, %s), ('Plain ✓ 😀', 1.5, NULL, NULL, NULL, NULL)",
              ["Learn FastAPI", "12.50", "2026-10-25", "2026-10-25 09:30:00", b"\x00\xff", '{"a": [1]}']).row_count == 2
    rows = my("SELECT id, title, price, due, at, blob_col, meta, done FROM tasks ORDER BY id")
    assert rows.columns == ["id", "title", "price", "due", "at", "blob_col", "meta", "done"]
    assert rows.rows[0][1:7] == ["Learn FastAPI", "12.50", "2026-10-25", "2026-10-25T09:30:00", "\\x00ff", '{"a": [1]}']
    assert rows.rows[1][1] == "Plain ✓ 😀"
    assert my("UPDATE tasks SET done = 1 WHERE title = %(t)s", {"t": "Learn FastAPI"}).row_count == 1
    assert my("SELECT COUNT(*) FROM tasks WHERE done = 1").rows == [[1]]
    assert my("DELETE FROM tasks WHERE done = 0").row_count == 1


@needs_mysql
def test_mysql_caps_timeouts_and_errors(mysql_db):
    with pytest.raises(errors.ResultTooLarge) as caught:
        my("SELECT seq FROM seq_1_to_1000")
    assert caught.value.extra["rows"] == [[1], [2], [3]]
    assert my("SELECT 1").rows == [[1]]   # the cut-off connection didn't leak into the next call
    with pytest.raises(errors.QueryTimeout):
        my("SELECT SLEEP(10)", lim=limits(timeout=2))
    with pytest.raises(errors.QueryError):
        my("SELEC 1")
    with pytest.raises(errors.QueryError):
        my("SELECT * FROM no_such_table")
    my("INSERT INTO tasks (id, title) VALUES (999, 'x')")
    with pytest.raises(errors.QueryError) as duplicate:
        my("INSERT INTO tasks (id, title) VALUES (999, 'x')")
    assert "Duplicate" in duplicate.value.message


@needs_mysql
def test_mysql_server_refuses_what_the_guard_would_block_anyway(mysql_db):
    """Defence in depth: even with the guard bypassed, the connection itself
    allows neither multi-statements nor LOAD DATA LOCAL."""
    with pytest.raises(errors.QueryError):
        my("SELECT 1; SELECT 2", guard=False)
    with pytest.raises(errors.QueryError):
        my("LOAD DATA LOCAL INFILE 'C:/Windows/win.ini' INTO TABLE tasks", guard=False)


@needs_mysql
def test_mysql_tls_and_credentials(mysql_db):
    version = run(MySqlConnector(POOL).test(mysql_target(), limits())).server_version
    assert version
    run(MySqlConnector(POOL).test(mysql_target(verify=False), limits(ca="")))
    with pytest.raises(errors.ConnectFailed):   # self-signed: not trusted by the system CAs
        run(MySqlConnector(POOL).test(mysql_target(), limits(ca="")))
    with pytest.raises(errors.ConnectFailed) as caught:
        run(MySqlConnector(POOL).test(mysql_target(user="nobody", password="Wr0ng-Passw0rd"), limits()))
    assert "Wr0ng-Passw0rd" not in caught.value.message


# ---- MongoDB ------------------------------------------------------------------------------


def mongo_target(user=None, password=None, verify=True):
    host, _, port = MONGO.partition(":")
    return Target("mongodb", "localhost", int(port), None, user, password, "codelab_test", verify, False,
                  (("localhost", int(port)),), {}, "localhost")


def mongo(operation="find", target=None, lim=None, **fields):
    request = MongoQueryRequest.model_validate({"engine": "mongodb", "url": "mongodb://x", "operation": operation,
                                                "database": "codelab_test", "collection": "orders", **fields})
    checked = MongoGuard().check(request, None)
    return run(MongoConnector(POOL).run(target or mongo_target(), checked, lim or limits()))


@pytest.fixture(scope="module")
def mongo_db():
    if not (MONGO and CA):
        pytest.skip("no MongoDB test server")
    mongo("delete_many", filter={})
    return True


@needs_mongo
def test_mongo_round_trip(mongo_db):
    inserted = mongo("insert_many", documents=[
        {"_id": {"$oid": "6719f0c2e4b0a1b2c3d4e5f6"}, "status": "paid", "amount": 500,
         "at": {"$date": "2026-10-25T09:30:00Z"}},
        {"status": "paid", "amount": 250}, {"status": "new", "amount": 99, "note": "✓"}])
    assert inserted.count == 3 and inserted.write["inserted_ids"][0] == {"$oid": "6719f0c2e4b0a1b2c3d4e5f6"}
    found = mongo(filter={"status": "paid"}, options={"sort": [["amount", -1]], "projection": {"_id": 0, "amount": 1}})
    assert found.documents == [{"amount": 500}, {"amount": 250}]
    one = mongo("find_one", filter={"_id": {"$oid": "6719f0c2e4b0a1b2c3d4e5f6"}})
    assert one.documents[0]["at"] == {"$date": "2026-10-25T09:30:00Z"}
    assert mongo("count_documents", filter={"status": "paid"}).count == 2
    grouped = mongo("aggregate", pipeline=[{"$group": {"_id": "$status", "total": {"$sum": "$amount"}}},
                                           {"$sort": {"_id": 1}}])
    assert grouped.documents == [{"_id": "new", "total": 99}, {"_id": "paid", "total": 750}]
    upserted = mongo("update_one", filter={"status": "refunded"}, update={"$set": {"amount": 0}},
                     options={"upsert": True})
    assert upserted.write["upserted_id"] is not None and upserted.write["matched_count"] == 0
    assert mongo("update_many", filter={"status": "paid"}, update={"$inc": {"amount": 1}}).count == 2
    assert mongo("delete_one", filter={"status": "new"}).write == {"deleted_count": 1}
    names = mongo("list_collection_names", collection=None)
    assert "orders" in names.documents


@needs_mongo
def test_mongo_caps_and_errors(mongo_db):
    mongo("insert_many", documents=[{"n": i} for i in range(20)])
    with pytest.raises(errors.ResultTooLarge) as caught:
        mongo(filter={"n": {"$gte": 0}}, options={"sort": [["n", 1]], "projection": {"_id": 0}})
    assert caught.value.extra["documents"] == [{"n": 0}, {"n": 1}, {"n": 2}]
    assert mongo(filter={"n": {"$gte": 0}}, options={"limit": 2}).count == 2
    with pytest.raises(errors.QueryError) as duplicate:
        mongo("insert_one", document={"_id": {"$oid": "6719f0c2e4b0a1b2c3d4e5f6"}})
    assert "duplicate key" in duplicate.value.message.lower()
    with pytest.raises(errors.QueryError):
        mongo(filter={"amount": {"$fakeoperator": 1}})
    with pytest.raises(errors.StatementBlocked):
        mongo(filter={"$where": "sleep(100)"})


@needs_mongo
def test_mongo_tls_and_credentials(mongo_db):
    assert run(MongoConnector(POOL).test(mongo_target(), limits())).server_version
    with pytest.raises(errors.ConnectFailed):   # self-signed: not trusted by the system CAs
        run(MongoConnector(POOL).test(mongo_target(), limits(ca="")))
    with pytest.raises(errors.ConnectFailed) as caught:
        run(MongoConnector(POOL).test(mongo_target(user="nobody", password="Wr0ng-Passw0rd"), limits()))
    assert "Wr0ng-Passw0rd" not in caught.value.message
