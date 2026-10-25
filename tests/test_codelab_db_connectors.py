"""CodeLab database proxy connectors.

MySQL and MongoDB: fakes of the driver objects, plus checks that the real
libraries accept exactly what the connectors pass them (PyMySQL marks TLS as
required with our context; MongoClient accepts every option).
Postgres: the real driver against a TLS-enabled throwaway server, set with
CODELAB_DB_PG_TEST=host:port and CODELAB_DB_PG_CA=<server certificate>;
skipped without them. Never point these at a production database."""

import asyncio
import datetime as dt
import os
import socket
import ssl
from types import SimpleNamespace

os.environ.setdefault("SUPABASE_URL", "postgresql://user:pass@localhost:5432/test")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

import pymongo
import pymysql
import pytest
from bson import ObjectId
from bson.errors import InvalidDocument
from pymongo import errors as mongo_errors

from codelab_db import errors
from codelab_db.infra.mongo_connector import MongoConnector, _client_options
from codelab_db.infra.mysql_connector import MySqlConnector, tls_context
from codelab_db.infra.pg_connector import PostgresConnector
from codelab_db.infra.renewable_executor import RenewableExecutor
from codelab_db.protocols import Limits, MongoOperation, SqlStatement, Target
from codelab_db.services.statement_guard import PostgresGuard

PASSWORD = "s3cr3t-Pa55word"
LIMITS = Limits(statement_timeout_seconds=10, connect_timeout_seconds=5, max_rows=3, max_bytes=100_000, ca_file=None)
POOL = RenewableExecutor(4)


def run(coroutine):
    return asyncio.run(coroutine)


def target(engine="mysql", verify=True, **overrides):
    values = dict(engine=engine, host="mysql-a.aivencloud.com", port=3306, address="3.6.1.1", user="learner",
                  password=PASSWORD, database="shop", tls_verify=verify, srv=False,
                  hosts=(("mysql-a.aivencloud.com", 3306),), options={}, domain="aivencloud.com")
    values.update(overrides)
    return Target(**values)


def statement(sql="SELECT 1", params=None, kind="execute"):
    return SqlStatement(sql, params, kind, sql.split()[0].upper())


# ---- MySQL --------------------------------------------------------------------------------


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None
        self.rowcount = -1
        self.sets = list(conn.extra_sets)

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        if sql.startswith("SET SESSION max_execution_time"):
            if self.conn.timeout_error:
                raise self.conn.timeout_error
            return 0
        if self.conn.query_error:
            raise self.conn.query_error
        if self.conn.rows is None:
            self.description, self.rowcount = None, self.conn.rowcount
        else:
            self.description = [(name,) for name in self.conn.columns]
        return 0

    def fetchmany(self, size):
        self.conn.fetch_sizes.append(size)
        return self.conn.rows[:size]

    def fetchall(self):
        return [("8.0.39",)]

    def nextset(self):
        self.conn.nextsets += 1
        return self.sets.pop() if self.sets else None


class FakeMySql:
    """Stands in for pymysql.connections.Connection."""

    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.executed, self.fetch_sizes = [], []
        self.columns, self.rows, self.rowcount = ["id", "name"], [(1, "a"), (2, "b")], 0
        self.query_error = self.timeout_error = self.connect_error = None
        self.extra_sets = []
        self.nextsets = 0
        self.committed = self.closed = self.forced = False
        self.sock = None
        FakeMySql.instances.append(self)
        if FakeMySql.configure:
            FakeMySql.configure(self)

    configure = None

    def connect(self, sock=None):
        self.sock = sock
        if self.connect_error:
            raise self.connect_error

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True

    def _force_close(self):
        self.forced = True


class FakeSocket:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


@pytest.fixture
def mysql(monkeypatch):
    FakeMySql.instances, FakeMySql.configure = [], None
    opened = []

    def open_socket(address, timeout):
        if getattr(open_socket, "error", None):
            raise open_socket.error
        sock = FakeSocket()
        opened.append((address, timeout, sock))
        return sock

    connector = MySqlConnector(POOL, open_socket=open_socket, connection_class=FakeMySql)
    return SimpleNamespace(connector=connector, opened=opened, open_socket=open_socket,
                           configure=lambda fn: setattr(FakeMySql, "configure", staticmethod(fn)))


def test_mysql_connects_pinned_with_tls_and_safe_options(mysql):
    result = run(mysql.connector.run(target(), statement("SELECT * FROM t"), LIMITS))
    conn = FakeMySql.instances[0]
    assert mysql.opened[0][:2] == (("3.6.1.1", 3306), 5) and conn.sock is mysql.opened[0][2]
    kwargs = conn.kwargs
    assert kwargs["host"] == "mysql-a.aivencloud.com" and kwargs["local_infile"] is False
    assert kwargs["client_flag"] == 0 and kwargs["defer_connect"] is True and kwargs["autocommit"] is False
    assert kwargs["cursorclass"] is pymysql.cursors.SSCursor and kwargs["read_timeout"] == 12
    assert isinstance(kwargs["ssl"], ssl.SSLContext) and kwargs["ssl"].check_hostname is True
    assert kwargs["ssl"].verify_mode == ssl.CERT_REQUIRED
    assert conn.executed[0] == ("SET SESSION max_execution_time = 10000", None)
    assert (result.columns, result.rows, result.row_count) == (["id", "name"], [[1, "a"], [2, "b"]], 2)
    assert conn.committed and conn.closed and not conn.forced and conn.fetch_sizes == [4]


def test_mysql_tls_without_verification_is_still_tls(mysql):
    run(mysql.connector.run(target(verify=False), statement(), LIMITS))
    context = FakeMySql.instances[0].kwargs["ssl"]
    assert isinstance(context, ssl.SSLContext) and context.verify_mode == ssl.CERT_NONE
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2


def test_real_pymysql_requires_tls_with_our_context():
    conn = pymysql.connections.Connection(host="h", user="u", password="p", ssl=tls_context(True, None),
                                          defer_connect=True, local_infile=False)
    assert conn.ssl is True and conn._ssl_required is True   # refuses a server without TLS


def test_mysql_write_and_params(mysql):
    mysql.configure(lambda c: setattr(c, "rows", None) or setattr(c, "rowcount", 3))
    result = run(mysql.connector.run(target(), statement("UPDATE t SET a = %s", [1]), LIMITS))
    assert (result.columns, result.rows, result.row_count) == ([], [], 3)
    assert FakeMySql.instances[0].executed[1] == ("UPDATE t SET a = %s", [1]) and FakeMySql.instances[0].committed


def test_mysql_truncation_never_commits(mysql):
    mysql.configure(lambda c: setattr(c, "rows", [(i, "x") for i in range(10)]))
    with pytest.raises(errors.ResultTooLarge) as caught:
        run(mysql.connector.run(target(), statement("SELECT * FROM big"), LIMITS))
    conn = FakeMySql.instances[0]
    assert caught.value.extra["rows"] == [[0, "x"], [1, "x"], [2, "x"]] and caught.value.extra["row_count"] == 3
    assert not conn.committed and conn.forced and not conn.closed


def test_mysql_drains_extra_result_sets_before_commit(mysql):
    mysql.configure(lambda c: setattr(c, "extra_sets", [True, True]))
    run(mysql.connector.run(target(), statement("CALL p()"), LIMITS))
    assert FakeMySql.instances[0].nextsets == 3 and FakeMySql.instances[0].committed


@pytest.mark.parametrize("error,expected", [
    (pymysql.err.OperationalError(3024, "Query execution was interrupted, max_execution_time exceeded"),
     errors.QueryTimeout),
    (pymysql.err.OperationalError(1317, "Query execution was interrupted"), errors.QueryTimeout),
    (pymysql.err.OperationalError(2013, "Lost connection to MySQL server during query"), errors.QueryTimeout),
    (socket.timeout("timed out"), errors.QueryTimeout),
    (pymysql.err.OperationalError(2006, "MySQL server has gone away"), errors.ConnectFailed),
    (ConnectionResetError("reset"), errors.ConnectFailed),
    (pymysql.err.ProgrammingError(1064, f"You have an error in your SQL syntax near '{PASSWORD}'"), errors.QueryError),
    (pymysql.err.OperationalError(1054, "Unknown column 'x' in 'field list'"), errors.QueryError),
    (pymysql.err.IntegrityError(1062, "Duplicate entry '1' for key 'PRIMARY'"), errors.QueryError),
    (pymysql.err.DataError(1406, "Data too long"), errors.QueryError), (RuntimeError("weird"), errors.QueryError),
    (pymysql.err.OperationalError(4166, "local infile capability is disabled"), errors.QueryError),
    (pymysql.err.OperationalError(3140, "Invalid JSON text"), errors.QueryError),
    (pymysql.err.OperationalError(2055, "Lost connection at 'reading initial communication packet'"),
     errors.ConnectFailed),
])
def test_mysql_query_errors_are_mapped(mysql, error, expected):
    mysql.configure(lambda c: setattr(c, "query_error", error))
    with pytest.raises(expected) as caught:
        run(mysql.connector.run(target(), statement(), LIMITS))
    assert PASSWORD not in caught.value.message
    conn = FakeMySql.instances[0]
    assert conn.forced and not conn.committed


def test_mysql_without_max_execution_time_still_runs(mysql):
    mysql.configure(lambda c: setattr(c, "timeout_error", pymysql.err.OperationalError(1193, "Unknown system variable")))
    assert run(mysql.connector.run(target(), statement(), LIMITS)).row_count == 2
    mysql.configure(lambda c: setattr(c, "timeout_error", pymysql.err.OperationalError(2013, "Lost connection")))
    with pytest.raises(errors.QueryTimeout):
        run(mysql.connector.run(target(), statement(), LIMITS))


@pytest.mark.parametrize("error", [
    pymysql.err.OperationalError(1045, f"Access denied for user 'learner' (using password: YES) {PASSWORD}"),
    pymysql.err.OperationalError(2003, "Can't connect to MySQL server"), ssl.SSLError("certificate verify failed"),
    pymysql.err.OperationalError(2026, "SSL is required but the server doesn't support it"), RuntimeError("odd"),
])
def test_mysql_connect_errors_close_the_socket(mysql, error):
    mysql.configure(lambda c: setattr(c, "connect_error", error))
    with pytest.raises(errors.ConnectFailed) as caught:
        run(mysql.connector.run(target(), statement(), LIMITS))
    assert PASSWORD not in caught.value.message and mysql.opened[0][2].closed


def test_mysql_socket_failure(mysql):
    mysql.open_socket.error = ConnectionRefusedError("refused")
    with pytest.raises(errors.ConnectFailed):
        run(mysql.connector.run(target(), statement(), LIMITS))
    with pytest.raises(errors.ConnectFailed):
        run(mysql.connector.test(target(), LIMITS))


def test_mysql_test_returns_the_version(mysql):
    assert run(mysql.connector.test(target(), LIMITS)).server_version == "8.0.39"
    assert FakeMySql.instances[0].closed


# ---- MongoDB --------------------------------------------------------------------------------


class Result(SimpleNamespace):
    pass


class FakeCollection:
    def __init__(self, client, name):
        self.client = client
        self.name = name

    def _maybe_fail(self, op):
        self.client.calls.append(op)
        self.client.timeouts.append(pymongo._csot.get_timeout())
        if self.client.error:
            raise self.client.error

    def find(self, query, **kwargs):
        self._maybe_fail(("find", query, kwargs))
        return iter(self.client.docs[:kwargs["limit"]])

    def count_documents(self, query, **kwargs):
        self._maybe_fail(("count", query, kwargs))
        return 42

    def aggregate(self, pipeline, **kwargs):
        self._maybe_fail(("aggregate", pipeline, kwargs))
        return iter(self.client.docs)

    def insert_one(self, document):
        self._maybe_fail(("insert_one", document))
        return Result(inserted_id=ObjectId("6719f0c2e4b0a1b2c3d4e5f6"))

    def insert_many(self, documents, ordered=True):
        self._maybe_fail(("insert_many", documents, ordered))
        return Result(inserted_ids=[ObjectId("6719f0c2e4b0a1b2c3d4e5f6"), 7])

    def update_one(self, query, update, upsert=False):
        self._maybe_fail(("update_one", query, update, upsert))
        return Result(matched_count=1, modified_count=1, upserted_id=None)

    def update_many(self, query, update, upsert=False):
        self._maybe_fail(("update_many", query, update, upsert))
        return Result(matched_count=0, modified_count=0, upserted_id=ObjectId("6719f0c2e4b0a1b2c3d4e5f7"))

    def delete_one(self, query):
        self._maybe_fail(("delete_one", query))
        return Result(deleted_count=1)

    def delete_many(self, query):
        self._maybe_fail(("delete_many", query))
        return Result(deleted_count=5)


class FakeDatabase:
    def __init__(self, client, name):
        self.client = client
        self.name = name

    def get_collection(self, name):
        return FakeCollection(self.client, name)

    def list_collection_names(self):
        self.client.calls.append(("list",))
        return ["orders", "carts"]


class FakeMongoClient:
    instances = []
    construct_error = None

    def __init__(self, host, **kwargs):
        if FakeMongoClient.construct_error:
            raise FakeMongoClient.construct_error
        self.host, self.kwargs = host, kwargs
        self.calls, self.timeouts = [], []
        self.error = None
        self.closed = False
        self.docs = [{"_id": ObjectId("6719f0c2e4b0a1b2c3d4e5f6"), "status": "paid", "amount": 500,
                      "at": dt.datetime(2026, 10, 25, 9, 30, tzinfo=dt.timezone.utc)}]
        FakeMongoClient.instances.append(self)
        if FakeMongoClient.configure:
            FakeMongoClient.configure(self)

    configure = None

    def get_database(self, name):
        return FakeDatabase(self, name)

    def server_info(self):
        if self.error:
            raise self.error
        return {"version": "8.0.4"}

    def close(self):
        self.closed = True


@pytest.fixture
def mongo():
    FakeMongoClient.instances, FakeMongoClient.configure, FakeMongoClient.construct_error = [], None, None
    return MongoConnector(POOL, client_class=FakeMongoClient)


SRV_HOSTS = (("ac-1-shard-00-00.ab12c.mongodb.net", 27017), ("ac-1-shard-00-01.ab12c.mongodb.net", 27017))


def mongo_target(**overrides):
    values = dict(engine="mongodb", host="ac-1-shard-00-00.ab12c.mongodb.net", port=27017, address=None,
                  user="learner", password=PASSWORD, database="shop", tls_verify=True, srv=True, hosts=SRV_HOSTS,
                  options={"retryWrites": "true", "w": "majority"}, domain="mongodb.net")
    values.update(overrides)
    return Target(**values)


def op(operation="find", **fields):
    values = dict(operation=operation, database="shop", collection="orders", filter=None, update=None, document=None,
                  documents=None, pipeline=None, projection=None, sort=None, limit=0, skip=0, upsert=False)
    values.update(fields)
    return MongoOperation(**values)


def test_mongo_client_is_built_from_checked_hosts_and_safe_options(mongo):
    run(mongo.run(mongo_target(), op(), LIMITS))
    client = FakeMongoClient.instances[0]
    # The checked SRV hosts as a seed list: pymongo never looks SRV up again.
    assert client.host == ["ac-1-shard-00-00.ab12c.mongodb.net:27017", "ac-1-shard-00-01.ab12c.mongodb.net:27017"]
    kwargs = client.kwargs
    assert kwargs["tls"] is True and "tlsAllowInvalidCertificates" not in kwargs and kwargs["maxPoolSize"] == 1
    assert (kwargs["username"], kwargs["password"]) == ("learner", PASSWORD)
    assert kwargs["retryWrites"] is True and kwargs["w"] == "majority" and "proxyHost" not in kwargs
    assert kwargs["serverSelectionTimeoutMS"] == 5000 and kwargs["socketTimeoutMS"] == 12000 and client.closed
    run(mongo.run(mongo_target(srv=False, hosts=(("a.x.mongodb.net", 27017), ("b.x.mongodb.net", 27017)),
                               tls_verify=False, user=None, options={"w": "1"}), op(), LIMITS))
    second = FakeMongoClient.instances[1]
    assert second.host == ["a.x.mongodb.net:27017", "b.x.mongodb.net:27017"]
    assert second.kwargs["tlsAllowInvalidCertificates"] is True and second.kwargs["w"] == 1
    assert "username" not in second.kwargs


def test_operations_only_go_to_checked_hosts():
    from codelab_db.infra.mongo_connector import checked_servers

    kwargs = _client_options(mongo_target(), LIMITS)
    servers = [SimpleNamespace(address=("AC-1-shard-00-00.ab12c.mongodb.net", 27017)),
               SimpleNamespace(address=("ac-1-shard-00-01.ab12c.mongodb.net", 27017)),
               SimpleNamespace(address=("announced-by-hello.example.com", 27017)),
               SimpleNamespace(address=("ac-1-shard-00-01.ab12c.mongodb.net", 27018))]
    assert kwargs["server_selector"](servers) == servers[:2]
    assert checked_servers(frozenset(), [object()]) == []   # a broken description selects nothing


def test_real_mongoclient_accepts_every_option():
    for options in ({}, {"retryWrites": "false", "w": "majority", "authSource": "admin",
                         "authMechanism": "SCRAM-SHA-256", "replicaSet": "atlas-x", "retryReads": "true",
                         "readPreference": "secondaryPreferred"}, {"loadBalanced": "false"}):
        kwargs = _client_options(mongo_target(srv=False, options=options), LIMITS)
        kwargs["connect"] = False
        client = pymongo.MongoClient(["a.x.mongodb.net:27017"], **kwargs)
        client.close()


def test_mongo_find_converts_extended_json_both_ways(mongo):
    result = run(mongo.run(mongo_target(), op(filter={"_id": {"$oid": "6719f0c2e4b0a1b2c3d4e5f6"},
                                                      "at": {"$gte": {"$date": "2026-01-01T00:00:00Z"}}},
                                              projection={"status": 1}, sort=[["at", -1]], skip=2, limit=2), LIMITS))
    name, query, kwargs = FakeMongoClient.instances[0].calls[0]
    assert isinstance(query["_id"], ObjectId) and isinstance(query["at"]["$gte"], dt.datetime)
    assert kwargs == {"projection": {"status": 1}, "skip": 2, "limit": 2, "sort": [("at", -1)], "max_time_ms": 10000}
    assert result.documents == [{"_id": {"$oid": "6719f0c2e4b0a1b2c3d4e5f6"}, "status": "paid", "amount": 500,
                                 "at": {"$date": "2026-10-25T09:30:00Z"}}]
    assert FakeMongoClient.instances[0].timeouts == [10]   # pymongo.timeout() wraps the operation


@pytest.mark.parametrize("requested,sent", [(0, 4), (2, 2), (3, 3), (10_000, 4)])
def test_mongo_find_limit_is_capped(mongo, requested, sent):
    run(mongo.run(mongo_target(), op(limit=requested), LIMITS))
    assert FakeMongoClient.instances[0].calls[0][2]["limit"] == sent


def test_mongo_truncation(mongo):
    FakeMongoClient.configure = lambda c: setattr(c, "docs", [{"n": i} for i in range(10)])
    with pytest.raises(errors.ResultTooLarge) as caught:
        run(mongo.run(mongo_target(), op(), LIMITS))
    assert caught.value.extra["documents"] == [{"n": 0}, {"n": 1}, {"n": 2}] and caught.value.extra["count"] == 3
    assert FakeMongoClient.instances[0].closed


def test_mongo_other_operations(mongo):
    assert run(mongo.run(mongo_target(), op("find_one"), LIMITS)).count == 1
    assert FakeMongoClient.instances[-1].calls[0][2]["limit"] == 1
    counted = run(mongo.run(mongo_target(), op("count_documents", filter={"a": 1}, skip=1, limit=5), LIMITS))
    assert counted.count == 42 and FakeMongoClient.instances[-1].calls[0][2] == {"maxTimeMS": 10000, "skip": 1,
                                                                                  "limit": 5}
    run(mongo.run(mongo_target(), op("aggregate", pipeline=[{"$match": {}}]), LIMITS))
    assert FakeMongoClient.instances[-1].calls[0][2] == {"maxTimeMS": 10000, "allowDiskUse": False, "batchSize": 4}
    inserted = run(mongo.run(mongo_target(), op("insert_one", document={"a": 1}), LIMITS))
    assert inserted.write == {"inserted_id": {"$oid": "6719f0c2e4b0a1b2c3d4e5f6"}}
    many = run(mongo.run(mongo_target(), op("insert_many", documents=[{"a": 1}, {"a": 2}]), LIMITS))
    assert many.count == 2 and many.write["inserted_ids"] == [{"$oid": "6719f0c2e4b0a1b2c3d4e5f6"}, 7]
    updated = run(mongo.run(mongo_target(), op("update_many", filter={}, update={"$set": {"a": 1}}, upsert=True),
                            LIMITS))
    assert updated.write == {"matched_count": 0, "modified_count": 0,
                             "upserted_id": {"$oid": "6719f0c2e4b0a1b2c3d4e5f7"}}
    assert FakeMongoClient.instances[-1].calls[0][3] is True
    assert run(mongo.run(mongo_target(), op("update_one", filter={}, update=[{"$set": {"a": 1}}]), LIMITS)).count == 1
    assert run(mongo.run(mongo_target(), op("delete_one", filter={"a": 1}), LIMITS)).write == {"deleted_count": 1}
    assert run(mongo.run(mongo_target(), op("delete_many"), LIMITS)).count == 5
    names = run(mongo.run(mongo_target(), op("list_collection_names", collection=None), LIMITS))
    assert names.documents == ["carts", "orders"]
    assert all(client.closed for client in FakeMongoClient.instances)


@pytest.mark.parametrize("error,expected", [
    (mongo_errors.ServerSelectionTimeoutError(f"No servers found {PASSWORD}, Timeout: 5s, Topology..."),
     errors.ConnectFailed),
    (mongo_errors.ExecutionTimeout("operation exceeded time limit", 50), errors.QueryTimeout),
    (mongo_errors.NetworkTimeout("timed out"), errors.QueryTimeout),
    (mongo_errors.OperationFailure("bad auth : authentication failed", 8000), errors.ConnectFailed),
    (mongo_errors.OperationFailure("Authentication failed.", 18), errors.ConnectFailed),
    (mongo_errors.AutoReconnect("connection closed"), errors.ConnectFailed),
    (mongo_errors.DuplicateKeyError("E11000 duplicate key", 11000), errors.QueryError),
    (mongo_errors.OperationFailure("unknown operator: $foo", 2), errors.QueryError),
    (mongo_errors.InvalidOperation("cannot do that"), errors.QueryError), (InvalidDocument("key '$x' must not start"),
                                                                          errors.QueryError),
    (OverflowError("MongoDB can only handle up to 8-byte ints"), errors.QueryError), (RuntimeError("odd"),
                                                                                      errors.QueryError),
])
def test_mongo_errors_are_mapped(mongo, error, expected):
    FakeMongoClient.configure = lambda c: setattr(c, "error", error)
    with pytest.raises(expected) as caught:
        run(mongo.run(mongo_target(), op(), LIMITS))
    assert PASSWORD not in caught.value.message and "Topology" not in caught.value.message
    assert FakeMongoClient.instances[0].closed


def test_mongo_bad_extended_json_is_a_bad_request(mongo):
    with pytest.raises(errors.BadRequest):
        run(mongo.run(mongo_target(), op(filter={"_id": {"$oid": "not-an-object-id"}}), LIMITS))
    assert FakeMongoClient.instances[0].closed


def test_mongo_client_construction_errors(mongo):
    FakeMongoClient.construct_error = mongo_errors.ConfigurationError("The DNS query name does not exist")
    with pytest.raises(errors.ConnectFailed):
        run(mongo.run(mongo_target(), op(), LIMITS))
    FakeMongoClient.construct_error = None


def test_mongo_test(mongo):
    assert run(mongo.test(mongo_target(), LIMITS)).server_version == "8.0.4"
    FakeMongoClient.configure = lambda c: setattr(c, "error", mongo_errors.OperationFailure("bad auth", 8000))
    with pytest.raises(errors.ConnectFailed):
        run(mongo.test(mongo_target(), LIMITS))
    FakeMongoClient.configure = lambda c: setattr(c, "error", mongo_errors.DuplicateKeyError("odd", 11000))
    with pytest.raises(errors.ConnectFailed):
        run(mongo.test(mongo_target(), LIMITS))


# ---- Postgres: connection settings (fake connect) ------------------------------------------------


class RecordingConnect:
    def __init__(self, error=None):
        self.kwargs = None
        self.error = error

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        raise ConnectionRefusedError("stop here")


def pg_target(**overrides):
    values = dict(engine="postgres", host="ep-a.us-east-2.aws.neon.tech", port=5432, address="3.6.1.1",
                  user="learner", password=PASSWORD, database="neondb", tls_verify=True, srv=False,
                  hosts=(("ep-a.us-east-2.aws.neon.tech", 5432),), options={"options": "endpoint=ep-a"},
                  domain="aws.neon.tech")
    values.update(overrides)
    return Target(**values)


def test_postgres_connection_settings():
    connect = RecordingConnect()
    with pytest.raises(errors.ConnectFailed):
        run(PostgresConnector(POOL, connect=connect).run(pg_target(), statement(), LIMITS))
    kwargs = connect.kwargs
    assert (kwargs["host"], kwargs["hostaddr"], kwargs["sslmode"], kwargs["sslrootcert"]) == (
        "ep-a.us-east-2.aws.neon.tech", "3.6.1.1", "verify-full", "system")
    assert kwargs["passfile"] == os.devnull and kwargs["gssencmode"] == "disable"
    assert kwargs["prepare_threshold"] is None and kwargs["autocommit"] is False
    assert kwargs["options"] == "endpoint=ep-a" and kwargs["connect_timeout"] == 5
    run_verify_off = RecordingConnect()
    with pytest.raises(errors.ConnectFailed):
        run(PostgresConnector(POOL, connect=run_verify_off).test(pg_target(tls_verify=False, options={}), LIMITS))
    assert run_verify_off.kwargs["sslmode"] == "require" and "sslrootcert" not in run_verify_off.kwargs


def test_postgres_connect_errors_hide_the_password():
    import psycopg

    connect = RecordingConnect(psycopg.OperationalError(f'connection failed: FATAL: password "{PASSWORD}" rejected'))
    with pytest.raises(errors.ConnectFailed) as caught:
        run(PostgresConnector(POOL, connect=connect).run(pg_target(), statement(), LIMITS))
    assert PASSWORD not in caught.value.message


# ---- Postgres: the real driver against a TLS test server ------------------------------------------


PG_SERVER = os.environ.get("CODELAB_DB_PG_TEST", "")
PG_CA = os.environ.get("CODELAB_DB_PG_CA", "")
needs_pg = pytest.mark.skipif(not (PG_SERVER and PG_CA), reason="CODELAB_DB_PG_TEST / CODELAB_DB_PG_CA not set")


def local_target(verify=True, database="postgres", user="postgres"):
    host, _, port = PG_SERVER.partition(":")
    return Target("postgres", "localhost", int(port), host, user, "", database, verify, False, (), {}, "localhost")


def pg_run(sql, params=None, limits=None, verify=True):
    checked = PostgresGuard(20480).check(SimpleNamespace(sql=sql, params=params), None)
    limits = limits or Limits(3, 5, 3, 100_000, PG_CA)
    return run(PostgresConnector(POOL).run(local_target(verify), checked, limits))


@needs_pg
def test_real_postgres_round_trip():
    pg_run("DROP TABLE IF EXISTS codelab_db_t")
    pg_run("CREATE TABLE codelab_db_t (id serial PRIMARY KEY, title text, done boolean DEFAULT false)")
    assert pg_run("INSERT INTO codelab_db_t (title) SELECT 'task ' || g FROM generate_series(1, 5) g").row_count == 5
    rows = pg_run("SELECT id, title FROM codelab_db_t WHERE id <= %s ORDER BY id", [2])
    assert (rows.columns, rows.rows, rows.row_count) == (["id", "title"], [[1, "task 1"], [2, "task 2"]], 2)
    empty = pg_run("SELECT id, title FROM codelab_db_t WHERE id < 0")
    assert (empty.columns, empty.rows) == (["id", "title"], [])
    returned = pg_run("UPDATE codelab_db_t SET done = true WHERE id = %(id)s RETURNING id, done", {"id": 3})
    assert returned.rows == [[3, True]] and returned.row_count == 1
    assert pg_run("DELETE FROM codelab_db_t WHERE id = 5").row_count == 1
    assert pg_run("SELECT count(*) AS n FROM codelab_db_t;").rows == [[4]]


@needs_pg
def test_real_postgres_types_become_json():
    result = pg_run("SELECT 12.50::numeric AS n, '2026-10-25 09:30+00'::timestamptz AS ts, '\\x00ff'::bytea AS b, "
                    "'00000000-0000-0000-0000-000000000001'::uuid AS u, '{\"a\": [1, null]}'::jsonb AS j, "
                    "ARRAY[1, 2] AS arr, interval '1 day 2 hours' AS i, '10.0.0.1'::inet AS ip, NULL AS z, "
                    "'NaN'::float8 AS nan, 'héllo ✓' AS text")
    row = dict(zip(result.columns, result.rows[0]))
    assert row["n"] == "12.50" and row["ts"].startswith("2026-10-25") and row["b"] == "\\x00ff"
    assert row["u"] == "00000000-0000-0000-0000-000000000001" and row["j"] == {"a": [1, None]}
    assert row["arr"] == [1, 2] and row["i"] == "1 day, 2:00:00" and row["ip"] == "10.0.0.1"
    assert row["z"] is None and row["nan"] == "nan" and row["text"] == "héllo ✓"


@needs_pg
def test_real_postgres_caps_timeouts_and_errors():
    with pytest.raises(errors.ResultTooLarge) as caught:
        pg_run("SELECT g FROM generate_series(1, 1000000) g")
    assert caught.value.extra["rows"] == [[1], [2], [3]]
    with pytest.raises(errors.ResultTooLarge):
        pg_run("SELECT repeat('x', 1000) FROM generate_series(1, 3)", limits=Limits(3, 5, 100, 1500, PG_CA))
    with pytest.raises(errors.QueryTimeout):
        pg_run("SELECT pg_sleep(5)")
    with pytest.raises(errors.QueryError) as syntax:
        pg_run("SELEC 1")
    assert "syntax error" in syntax.value.message
    with pytest.raises(errors.QueryError):
        pg_run("SELECT 1 / 0")
    with pytest.raises(errors.QueryError):
        pg_run("SELECT %s::int", ["not a number"])


@needs_pg
def test_real_postgres_statements_outside_a_transaction():
    pg_run("DROP TABLE IF EXISTS codelab_db_v")
    pg_run("CREATE TABLE codelab_db_v (id int)")
    assert pg_run("VACUUM codelab_db_v").row_count == 0
    assert pg_run("CREATE INDEX CONCURRENTLY codelab_db_v_id ON codelab_db_v (id)").row_count == 0
    assert pg_run("DROP INDEX CONCURRENTLY codelab_db_v_id").row_count == 0
    for statement in ("COMMIT", "ROLLBACK", "BEGIN", "END"):   # harmless, never a confusing error
        assert pg_run(statement).columns == []
    assert pg_run("SELECT 1 AS still_fine").rows == [[1]]


@needs_pg
def test_real_postgres_truncated_write_is_rolled_back():
    pg_run("DROP TABLE IF EXISTS codelab_db_w")
    pg_run("CREATE TABLE codelab_db_w (id int)")
    with pytest.raises(errors.ResultTooLarge):
        pg_run("INSERT INTO codelab_db_w SELECT g FROM generate_series(1, 10) g RETURNING id")
    assert pg_run("SELECT count(*) FROM codelab_db_w").rows == [[0]]


@needs_pg
def test_real_postgres_tls_verification_is_enforced():
    assert run(PostgresConnector(POOL).test(local_target(True), Limits(3, 5, 3, 100_000, PG_CA))).server_version
    with pytest.raises(errors.ConnectFailed):   # self-signed: not trusted by the system CAs
        run(PostgresConnector(POOL).test(local_target(True), Limits(3, 5, 3, 100_000, None)))
    with pytest.raises(errors.ConnectFailed):
        run(PostgresConnector(POOL).test(local_target(True, database="does_not_exist"),
                                         Limits(3, 5, 3, 100_000, PG_CA)))
