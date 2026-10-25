"""CodeLab database proxy: unit tests for every piece that needs no network -
the SQL lexer and statement guards, URL validation and the host allowlist,
DNS checks (fake lookups), JSON conversion, redaction, limits, request
parsing, settings and the feature's code rules."""

import ast
import asyncio
import ipaddress
import json
import logging
import os
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("SUPABASE_URL", "postgresql://user:pass@localhost:5432/test")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

import pytest
from pydantic import SecretStr

from codelab_db import errors
from codelab_db.infra.dns_host_resolver import DnsHostResolver, is_public_address
from codelab_db.infra.json_host_allowlist import load_host_allowlist, parse_allowlist
from codelab_db.infra.memory_limiter import MemoryLimiter
from codelab_db.infra.renewable_executor import RenewableExecutor
from codelab_db.schemas import MongoQueryRequest, SqlQueryRequest, parse_query_request, parse_test_request
from codelab_db.services.json_values import RowCollector, to_json_value
from codelab_db.services.redaction import CredentialLogFilter, redact
from codelab_db.services.sql_lexer import lex
from codelab_db.services.statement_guard import MongoGuard, MySqlGuard, PostgresGuard
from codelab_db.services.target_validator import TargetValidator
from codelab_db.settings import DbProxySettings

PACKAGE_DIR = Path(__file__).resolve().parent.parent / "codelab_db"
HOSTS_FILE = PACKAGE_DIR / "hosts.json"
NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
USER = uuid.uuid4()
SUPABASE = "postgresql://postgres.abcd:s3cr3t-pass@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"


def run(coroutine):
    return asyncio.run(coroutine)


def sql(text, params=None):
    return SimpleNamespace(sql=text, params=params)


def pg(text, params=None):
    return PostgresGuard(20480).check(sql(text, params), None)


def my(text, params=None):
    return MySqlGuard(20480).check(sql(text, params), None)


# ---- lexer ----------------------------------------------------------------------------


@pytest.mark.parametrize("text,statements", [
    ("SELECT 1", 1), ("SELECT 1;", 1), ("SELECT 1;;  ", 1), ("SELECT ';' AS semi", 1), ("SELECT $$a;b$$", 1),
    ("SELECT $tag$ ; $tag$", 1), ("SELECT 'it''s; fine'", 1), ("SELECT E'\\';'", 1), ('SELECT "odd;name" FROM t', 1),
    ("-- note ;\nSELECT 1", 1), ("/* a /* nested ; */ b */ SELECT 1", 1), ("SELECT 1; SELECT 2", 2),
    ("SELECT '\\'; SELECT 2", 2), ("SELECT $1, $2", 1), ("", 0), ("-- only a comment", 0), (" ; ; ", 0),
])
def test_postgres_lexer_counts_statements_like_the_server(text, statements):
    assert lex(text, "postgres").statements == statements


@pytest.mark.parametrize("text,statements", [
    ("SELECT 'it\\'s; still a string'", 1), ('SELECT "double ; quoted"', 1), ("SELECT 1 # comment ; x", 1),
    ("SELECT 1 -- comment ; x", 1), ("SELECT 1--1; SELECT 2", 2), ("SELECT `odd;name` FROM t", 1),
    ("SELECT /* plain ; comment */ 1", 1), ("SELECT /*! 1; SELECT 2 */", 2), ("SELECT /*!50001 1 */", 1),
])
def test_mysql_lexer_counts_statements_like_the_server(text, statements):
    assert lex(text, "mysql").statements == statements


@pytest.mark.parametrize("dialect,text", [
    ("postgres", "SELECT 'abc"), ("postgres", "SELECT /* x"), ("postgres", "SELECT /* a /* b */"),
    ("postgres", "SELECT $$abc"), ("postgres", 'SELECT "abc'), ("postgres", "SELECT E'abc\\'"),
    ("mysql", "SELECT 'abc"), ("mysql", "SELECT `x"), ("mysql", "SELECT /*! 1"), ("mysql", "SELECT /* x"),
])
def test_lexer_refuses_unterminated_quotes_and_comments(dialect, text):
    with pytest.raises(errors.BadRequest):
        lex(text, dialect)


def test_lexer_views():
    lexed = lex("SELECT 'COPY' AS a, \"PG_READ_FILE\"(1) -- COPY\n", "postgres")
    assert "COPY" not in lexed.code and "'?'" in lexed.code
    assert "PG_READ_FILE(1)" in lexed.text and "COPY" in lexed.text   # string content kept, comment gone


# ---- Postgres guard -------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "SELECT 1", "select * from tasks where done = %s", "SELECT 'copy that' AS note", "SELECT 1 -- COPY t TO PROGRAM",
    "/* pg_read_file('x') */ SELECT 1", "SELECT $$it's$$", "INSERT INTO notes(body) VALUES ('grant all')",
    "CREATE TABLE tasks (id serial primary key, title text)", "SELECT E'\\'; COPY t TO PROGRAM ''x''; --'",
    "WITH x AS (SELECT 1) SELECT * FROM x", "SELECT pg_read_file_note FROM t", "DO $$ BEGIN RAISE NOTICE 'hi'; END $$",
    "CREATE FUNCTION add(a int, b int) RETURNS int LANGUAGE sql AS $$ SELECT a + b $$",
    "CREATE FUNCTION f() RETURNS void LANGUAGE plpgsql AS $$ BEGIN PERFORM 1; END $$",
    "CREATE FUNCTION f() RETURNS int AS $$ SELECT 1 $$ LANGUAGE 'sql'",
    "CREATE FUNCTION f() RETURNS int AS 'SELECT 1' LANGUAGE \"plpgsql\"",
    "INSERT INTO notes VALUES ('LANGUAGE c is fast')",
])
def test_postgres_guard_allows_normal_sql(text):
    assert pg(text).sql


@pytest.mark.parametrize("text,blocked", [
    ("SELECT 1; SELECT 2", "More than one statement per request"),
    ("SELECT '\\'; COPY t TO PROGRAM 'x'; --'", "More than one statement per request"),
    ("COPY t TO PROGRAM 'rm -rf /'", "COPY"), ("copy t from '/etc/passwd'", "COPY"),
    ("COPY (SELECT 1) TO STDOUT", "COPY"), ("SELECT pg_read_file('/etc/passwd')", "pg_read_file()"),
    ("SELECT pg_catalog.pg_read_file('x')", "pg_read_file()"), ('SELECT "pg_read_file"(\'x\')', "pg_read_file()"),
    ("SELECT PG_READ_FILE ('x')", "pg_read_file()"), ("select pg_read_binary_file('x')", "pg_read_binary_file()"),
    ("select pg_ls_dir('.')", "pg_ls_dir()"), ("SELECT pg_ls_logdir()", "pg_ls_logdir()"),
    ("SELECT pg_stat_file('x')", "pg_stat_file()"), ("SELECT lo_import('/etc/passwd')", "lo_import()"),
    ("SELECT lo_export(1, '/tmp/x')", "lo_export()"), ("SELECT * FROM dblink('host=x', 'SELECT 1') AS t(a int)",
                                                        "dblink()"),
    ("SELECT dblink_connect('host=10.0.0.1')", "dblink_connect()"), ("CREATE EXTENSION dblink", "CREATE EXTENSION"),
    ("create   extension\n\tpostgres_fdw", "CREATE EXTENSION"), ("ALTER SYSTEM SET work_mem = '1GB'", "ALTER SYSTEM"),
    ("CREATE ROLE admin SUPERUSER", "CREATE/ALTER ROLE"), ("ALTER ROLE postgres PASSWORD 'x'", "CREATE/ALTER ROLE"),
    ("CREATE USER bob", "CREATE/ALTER ROLE"), ("CREATE SERVER s FOREIGN DATA WRAPPER postgres_fdw", "Foreign servers"),
    ("CREATE FOREIGN TABLE f (a int) SERVER s", "Foreign servers"),
    ("IMPORT FOREIGN SCHEMA public FROM SERVER s INTO x", "IMPORT FOREIGN SCHEMA"), ("LOAD 'auto_explain'", "LOAD"),
    ("CREATE FUNCTION f() RETURNS int AS 'x' LANGUAGE c", "Untrusted function languages"),
    ("CREATE FUNCTION f() RETURNS int AS $$ return 1 $$ LANGUAGE plpython3u", "Untrusted function languages"),
    ("CREATE FUNCTION f() RETURNS int AS 'x' LANGUAGE \"c\"", "Untrusted function languages"),
    ("CREATE FUNCTION f() RETURNS int AS 'x' LANGUAGE 'c'", "Untrusted function languages"),
    ("CREATE OR REPLACE PROCEDURE p() LANGUAGE \"plpython3u\" AS 'x'", "Untrusted function languages"),
    ("DO 'BEGIN EXECUTE ''COPY t TO PROGRAM ''''id''''''; END'", "COPY"),
    ("DO LANGUAGE plpgsql 'BEGIN EXECUTE ''ALTER SYSTEM SET x = 1''; END'", "ALTER SYSTEM"),
    ("CREATE FUNCTION f() RETURNS void AS 'BEGIN EXECUTE ''CREATE ROLE r''; END' LANGUAGE plpgsql",
     "CREATE/ALTER ROLE"),
    ("DO $$ BEGIN EXECUTE 'COPY t TO PROGRAM ''x'''; END $$", "COPY"),
    ("DO $body$ BEGIN PERFORM pg_read_file('x'); END $body$", "pg_read_file()"),
    ("CREATE FUNCTION f() RETURNS text AS $$ SELECT pg_read_file('x') $$ LANGUAGE sql", "pg_read_file()"),
])
def test_postgres_guard_blocks(text, blocked):
    with pytest.raises(errors.StatementBlocked) as caught:
        pg(text)
    assert caught.value.extra == {"blocked": blocked} and caught.value.http_status == 403


@pytest.mark.parametrize("text", ["", "   ", "-- nothing\n", ";", "SELECT 'x\x00'", "SELECT 'unterminated"])
def test_postgres_guard_refuses_empty_or_broken_sql(text):
    with pytest.raises(errors.BadRequest):
        pg(text)


def test_sql_size_limit_counts_bytes():
    with pytest.raises(errors.BadRequest):
        PostgresGuard(64).check(sql("SELECT '" + "é" * 40 + "'"), None)   # 40 chars, 80+ bytes
    assert PostgresGuard(64).check(sql("SELECT '" + "e" * 40 + "'"), None)


@pytest.mark.parametrize("text,kind,statement_type", [
    ("SELECT 1", "cursor", "SELECT"), ("select * from t", "cursor", "SELECT"), ("VALUES (1), (2)", "cursor", "VALUES"),
    ("TABLE t", "cursor", "TABLE"), ("(SELECT 1)", "cursor", "SELECT"),
    ("WITH x AS (SELECT 1) SELECT * FROM x", "cursor", "WITH"),
    ("WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 3) SELECT * FROM r", "cursor", "WITH"),
    ("SELECT * FROM t FOR SHARE", "cursor", "SELECT"),
    ("SELECT * INTO copy_of_t FROM t", "execute", "SELECT"), ("SELECT * FROM t FOR UPDATE", "execute", "SELECT"),
    ("WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d", "execute", "WITH"),
    ("WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x", "execute", "WITH"),
    ("INSERT INTO t VALUES (1) RETURNING id", "execute", "INSERT"), ("UPDATE t SET a = 1", "execute", "UPDATE"),
    ("DELETE FROM t", "execute", "DELETE"), ("SHOW server_version", "execute", "SHOW"),
    ("EXPLAIN SELECT 1", "execute", "EXPLAIN"), ("CREATE TABLE t (a int)", "execute", "CREATE"),
    ("-- lead\nSELECT 1", "cursor", "SELECT"),
    ("VACUUM tasks", "autocommit", "VACUUM"), ("vacuum analyze", "autocommit", "VACUUM"),
    ("CREATE INDEX CONCURRENTLY i ON t (a)", "autocommit", "CREATE"),
    ("DROP INDEX CONCURRENTLY i", "autocommit", "DROP"), ("REINDEX TABLE t", "autocommit", "REINDEX"),
    ("CREATE DATABASE shop", "autocommit", "CREATE"), ("DROP DATABASE shop", "autocommit", "DROP"),
    ("COMMIT", "autocommit", "COMMIT"), ("BEGIN", "autocommit", "BEGIN"), ("ROLLBACK", "autocommit", "ROLLBACK"),
    ("START TRANSACTION", "autocommit", "START"), ("END", "autocommit", "END"),
    ("SAVEPOINT a", "autocommit", "SAVEPOINT"), ("PREPARE TRANSACTION 'x'", "autocommit", "PREPARE"),
    ("CREATE INDEX i ON t (a)", "execute", "CREATE"), ("PREPARE q AS SELECT 1", "execute", "PREPARE"),
])
def test_postgres_statements_are_classified_for_the_connector(text, kind, statement_type):
    statement = pg(text)
    assert (statement.kind, statement.statement_type) == (kind, statement_type)


def test_separators_are_removed_but_string_semicolons_kept():
    assert pg("SELECT 1;").sql == "SELECT 1"
    assert pg("SELECT ';' AS semi ;").sql == "SELECT ';' AS semi"
    assert pg(";SELECT 1").sql == "SELECT 1"


def test_params_are_normalised():
    assert pg("SELECT %s", [1]).params == [1]
    assert pg("SELECT %(a)s", {"a": 1}).params == {"a": 1}
    assert pg("SELECT 1", []).params is None and pg("SELECT 1", {}).params is None and pg("SELECT 1").params is None


# ---- MySQL guard ----------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "SELECT 1", "SELECT * FROM orders WHERE id = %s", "SELECT 1 # LOAD DATA INFILE", "SELECT 1 -- LOAD DATA",
    "SELECT /* LOAD_FILE('x') */ 1", "SELECT 'it\\'s fine'", "SET @x = 1", "SET SESSION sql_mode = ''",
    "SELECT 'grant me access'", "INSERT INTO t (note) VALUES ('load data later')", "SELECT 1--1",
    "CREATE TABLE orders (id INT PRIMARY KEY AUTO_INCREMENT, amount DECIMAL(10,2))",
])
def test_mysql_guard_allows_normal_sql(text):
    assert my(text).kind == "execute"


@pytest.mark.parametrize("text,blocked", [
    ("LOAD DATA LOCAL INFILE '/etc/passwd' INTO TABLE t", "LOAD DATA"), ("load xml infile 'x' into table t", "LOAD DATA"),
    ("SELECT * FROM t INTO OUTFILE '/tmp/x'", "SELECT ... INTO OUTFILE"),
    ("SELECT 1 INTO DUMPFILE '/tmp/x'", "SELECT ... INTO OUTFILE"), ("SELECT LOAD_FILE('/etc/passwd')", "load_file()"),
    ("SELECT `LOAD_FILE`('/etc/passwd')", "load_file()"), ("SELECT load_file ('/x')", "load_file()"),
    ("SELECT 1--1, LOAD_FILE('/etc/passwd')", "load_file()"), ("SELECT /*! LOAD_FILE('/x') */", "load_file()"),
    ("SELECT /*!50000 1 */ INTO OUTFILE '/x'", "SELECT ... INTO OUTFILE"),
    ("SET GLOBAL max_connections = 1", "SET GLOBAL"), ("SET @@global.max_connections = 1", "SET GLOBAL"),
    ("SET PERSIST max_connections = 1", "SET GLOBAL"), ("GRANT ALL ON *.* TO 'u'@'%'", "GRANT"),
    ("CREATE USER 'u'@'%' IDENTIFIED BY 'p'", "CREATE USER"), ("ALTER USER root IDENTIFIED BY 'x'", "CREATE USER"),
    ("INSTALL PLUGIN x SONAME 'y.so'", "INSTALL PLUGIN"), ("UNINSTALL PLUGIN x", "UNINSTALL PLUGIN"),
    ("PREPARE s FROM 'SELECT LOAD_FILE(1)'", "PREPARE (dynamic SQL)"), ("SHUTDOWN", "SHUTDOWN"),
    ("SELECT 1; SELECT 2", "More than one statement per request"),
    ("SELECT /*! 1; DROP TABLE t */", "More than one statement per request"),
])
def test_mysql_guard_blocks(text, blocked):
    with pytest.raises(errors.StatementBlocked) as caught:
        my(text)
    assert caught.value.extra == {"blocked": blocked}


# ---- Mongo guard ----------------------------------------------------------------------


def mongo(operation="find", **fields):
    body = {"engine": "mongodb", "url": "mongodb+srv://u:p@cluster0.ab12c.mongodb.net/shop", "operation": operation,
            "collection": "orders", **fields}
    return MongoQueryRequest.model_validate(body)


def mongo_check(operation="find", database_from_url="shop", **fields):
    return MongoGuard().check(mongo(operation, **fields), database_from_url)


def test_mongo_guard_builds_the_operation():
    op = mongo_check(filter={"status": "paid"}, options={"limit": 20, "sort": [["created_at", -1]], "skip": 5})
    assert (op.database, op.collection, op.limit, op.skip, op.sort) == ("shop", "orders", 20, 5, [["created_at", -1]])
    assert mongo_check(options={"sort": {"a": 1, "b": -1}}).sort == [["a", 1], ["b", -1]]
    assert mongo_check(database="other").database == "other"
    assert mongo_check("list_collection_names", collection=None).collection is None
    assert mongo_check("update_one", filter={}, update={"$set": {"a": 1}}, options={"upsert": True}).upsert is True
    assert mongo_check("find", options={"upsert": True}).upsert is False


@pytest.mark.parametrize("database", ["admin", "ADMIN", "local", "config"])
def test_mongo_system_databases_are_blocked(database):
    with pytest.raises(errors.StatementBlocked):
        mongo_check(database=database)


@pytest.mark.parametrize("kwargs", [
    {"database_from_url": None}, {"database": "a.b"}, {"database": "a/b"}, {"database": "a$b"}, {"database": "a b"},
    {"collection": None}, {"collection": ""},
])
def test_mongo_names_are_validated(kwargs):
    with pytest.raises(errors.BadRequest):
        mongo_check(**kwargs)


@pytest.mark.parametrize("collection", ["system.users", "a$b"])
def test_mongo_system_collections_are_blocked(collection):
    with pytest.raises(errors.StatementBlocked):
        mongo_check(collection=collection)


@pytest.mark.parametrize("operation,fields", [
    ("insert_one", {}), ("insert_many", {}), ("insert_many", {"documents": []}), ("update_one", {}),
    ("update_many", {"update": {}}), ("aggregate", {}),
])
def test_mongo_operations_need_their_fields(operation, fields):
    with pytest.raises(errors.BadRequest):
        mongo_check(operation, **fields)


@pytest.mark.parametrize("operation,fields", [
    ("find", {"filter": {"$where": "sleep(1000)"}}),
    ("find", {"filter": {"$and": [{"a": 1}, {"$where": "1"}]}}),
    ("find", {"filter": {"$expr": {"$function": {"body": "x", "args": [], "lang": "js"}}}}),
    ("aggregate", {"pipeline": [{"$match": {}}, {"$out": "copy"}]}),
    ("aggregate", {"pipeline": [{"$merge": {"into": "x"}}]}),
    ("aggregate", {"pipeline": [{"$facet": {"a": [{"$out": "x"}]}}]}),
    ("aggregate", {"pipeline": [{"$group": {"_id": 1, "x": {"$accumulator": {}}}}]}),
    ("aggregate", {"pipeline": [{"$lookup": {"from": {"db": "admin", "coll": "system.users"}, "as": "x"}}]}),
    ("aggregate", {"pipeline": [{"$lookup": {"from": "x", "pipeline": [{"$merge": "y"}], "as": "z"}}]}),
    ("update_one", {"filter": {}, "update": [{"$set": {"a": {"$function": {}}}}]}),
    ("insert_one", {"document": {"$where": 1}}),
    ("find", {"options": {"projection": {"x": {"$function": {}}}}}),
])
def test_mongo_blocked_operators_are_found_anywhere(operation, fields):
    with pytest.raises(errors.StatementBlocked):
        mongo_check(operation, **fields)


# ---- host allowlist ----------------------------------------------------------------------


def allowlist():
    return load_host_allowlist(str(HOSTS_FILE))


@pytest.mark.parametrize("engine,host,allowed", [
    ("postgres", "aws-0-ap-south-1.pooler.supabase.com", True), ("postgres", "db.abcd.supabase.co", True),
    ("postgres", "ep-cool-name-123456.ap-southeast-1.aws.neon.tech", True),
    ("postgres", "ep-x.eastus2.azure.neon.tech", True), ("postgres", "pooler.supabase.com", False),
    ("postgres", "a.b.pooler.supabase.com", False), ("postgres", "db.a.b.supabase.co", False),
    ("postgres", "x.pooler.supabase.com.evil.com", False), ("postgres", "evilpooler.supabase.com", False),
    ("postgres", "x.neon.tech", False), ("postgres", "localhost", False),
    ("mysql", "mysql-abc-proj.aivencloud.com", True), ("mysql", "gateway01.ap-southeast-1.prod.aws.tidbcloud.com", True),
    ("mysql", "aws.connect.psdb.cloud", True), ("mysql", "aws-0-ap-south-1.pooler.supabase.com", False),
    ("mongodb", "cluster0.ab12c.mongodb.net", True), ("mongodb", "ac-x-shard-00-00.ab12c.mongodb.net", True),
    ("mongodb", "mongodb.net", False), ("mongodb", "x.mongodb.net", False), ("mongodb", "a.b.c.mongodb.net", False),
])
def test_allowlist_matches_one_label_per_wildcard(engine, host, allowed):
    assert (allowlist().match(engine, host) is not None) is allowed


def test_allowlist_rules_carry_tls_and_a_loggable_domain():
    rule = allowlist().match("postgres", "aws-0-ap-south-1.pooler.supabase.com")
    assert (rule.domain, rule.tls_verify) == ("pooler.supabase.com", False)
    neon = allowlist().match("postgres", "ep-a.us-east-2.aws.neon.tech")
    assert (neon.domain, neon.tls_verify) == ("aws.neon.tech", True)
    described = allowlist().describe()
    assert set(described) == {"postgres", "mysql", "mongodb"} and described["postgres"]["ports"] == [5432, 6543]


def good_allowlist() -> dict:
    return json.loads(HOSTS_FILE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("mutate", [
    lambda d: d.pop("mysql"), lambda d: d.update(redis={}), lambda d: d["postgres"].update(ports=[]),
    lambda d: d["postgres"].update(ports=[True]), lambda d: d["postgres"].update(ports=[0]),
    lambda d: d["postgres"].update(ports=[70000]), lambda d: d["postgres"].update(default_port=1),
    lambda d: d["postgres"].update(hosts=[]), lambda d: d["postgres"]["hosts"][0].update(pattern="*.COM.example"),
    lambda d: d["postgres"]["hosts"][0].update(pattern="*.com"), lambda d: d["postgres"]["hosts"][0].update(pattern="*.*.*"),
    lambda d: d["postgres"]["hosts"][0].update(pattern="x.supabase.*"),
    lambda d: d["postgres"]["hosts"][0].update(pattern="a.*.b_c.com"),
    lambda d: d["postgres"]["hosts"][0].pop("tls_verify"), lambda d: d["postgres"]["hosts"][0].update(label=" "),
    lambda d: d["postgres"]["hosts"].append(dict(d["postgres"]["hosts"][0])),
    lambda d: d["postgres"]["hosts"].append("x"), lambda d: d.update(postgres=[]),
])
def test_allowlist_validation_refuses_bad_files(mutate):
    data = good_allowlist()
    mutate(data)
    with pytest.raises(errors.RegistryInvalid):
        parse_allowlist(data)


def test_allowlist_file_errors(tmp_path):
    broken = tmp_path / "hosts.json"
    broken.write_text("{not json", encoding="utf-8")
    for path in (broken, tmp_path / "missing.json"):
        with pytest.raises(errors.RegistryInvalid):
            load_host_allowlist(str(path))
    with pytest.raises(errors.RegistryInvalid):
        parse_allowlist([])


# ---- URL validation ----------------------------------------------------------------------


class FakeResolver:
    def __init__(self, addresses=None, srv=None, error=None, txt=None, delay=0.0):
        self.addresses = addresses or {}
        self.srv = srv or []
        self.txt = txt or []
        self.error = error
        self.delay = delay
        self.calls = []

    async def resolve_public(self, host):
        self.calls.append(host)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return list(self.addresses.get(host, ["104.18.1.1"]))

    async def resolve_srv(self, name):
        self.calls.append(name)
        return list(self.srv)

    async def resolve_txt(self, name):
        self.calls.append(("txt", name))
        return list(self.txt)


def validator(resolver=None):
    return TargetValidator(allowlist(), resolver or FakeResolver())


def parse(engine, url):
    return validator().parse(engine, url)


def test_supabase_url_parses():
    parsed = parse("postgres", SUPABASE)
    assert parsed.hosts == (("aws-0-ap-south-1.pooler.supabase.com", 5432),)
    assert (parsed.user, parsed.password, parsed.database) == ("postgres.abcd", "s3cr3t-pass", "postgres")
    assert "s3cr3t" not in repr(parsed)


@pytest.mark.parametrize("url,expected", [
    ("postgres://u:p@aws-0-x.pooler.supabase.com:6543/db", (("aws-0-x.pooler.supabase.com", 6543), "db", {})),
    ("postgresql://u:p%40ss%2Fw@aws-0-x.pooler.supabase.com/postgres",
     (("aws-0-x.pooler.supabase.com", 5432), "postgres", {})),
    ("postgresql://u:p@ep-a-1.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require",
     (("ep-a-1.us-east-2.aws.neon.tech", 5432), "neondb", {"channel_binding": "require"})),
    ("postgresql://u:p@ep-a-1.us-east-2.aws.neon.tech/neondb?options=endpoint%3Dep-a-1",
     (("ep-a-1.us-east-2.aws.neon.tech", 5432), "neondb", {"options": "endpoint=ep-a-1"})),
    ("postgresql://u:p@AWS-0-X.POOLER.SUPABASE.COM/postgres?application_name=x&connect_timeout=3",
     (("aws-0-x.pooler.supabase.com", 5432), "postgres", {})),
    ("postgresql://u@db.abcd.supabase.co", (("db.abcd.supabase.co", 5432), None, {})),
])
def test_postgres_urls(url, expected):
    parsed = parse("postgres", url)
    assert (parsed.hosts[0], parsed.database, parsed.options) == expected


def test_percent_encoded_credentials_are_decoded():
    parsed = parse("postgres", "postgresql://u%2Bme:p%40ss%2Fw%3A@aws-0-x.pooler.supabase.com/postgres")
    assert (parsed.user, parsed.password) == ("u+me", "p@ss/w:")


@pytest.mark.parametrize("url", [
    "mysql://u:p@mysql-abc-proj.aivencloud.com:3306/defaultdb?ssl-mode=REQUIRED",
    "mysql://u:p@gateway01.ap-southeast-1.prod.aws.tidbcloud.com:4000/test",
    "mysql://u:p@aws.connect.psdb.cloud/db?ssl=%7B%22rejectUnauthorized%22%3Atrue%7D",
    "mysql://u:p@aws.connect.psdb.cloud/db?sslaccept=strict",
])
def test_mysql_urls(url):
    assert parse("mysql", url).options == {}


def test_mongo_urls():
    srv = parse("mongodb", "mongodb+srv://learner:pw@cluster0.ab12c.mongodb.net/shop"
                           "?retryWrites=true&w=majority&appName=Cluster0")
    assert (srv.srv, srv.hosts, srv.database) == (True, (("cluster0.ab12c.mongodb.net", None),), "shop")
    assert srv.options == {"retryWrites": "true", "w": "majority"}
    seeds = parse("mongodb", "mongodb://u:p@ac-1-shard-00-00.ab12c.mongodb.net:27017,"
                             "ac-1-shard-00-01.ab12c.mongodb.net:27017/?replicaSet=atlas-x&authSource=admin&tls=true")
    assert len(seeds.hosts) == 2 and seeds.options == {"replicaSet": "atlas-x", "authSource": "admin"}
    assert parse("mongodb", "mongodb+srv://cluster0.ab12c.mongodb.net").user is None


@pytest.mark.parametrize("engine,url,error", [
    ("postgres", "mysql://u:p@aws-0-x.pooler.supabase.com/db", errors.BadUrl),
    ("postgres", "aws-0-x.pooler.supabase.com:5432", errors.BadUrl),
    ("postgres", "http://u:p@aws-0-x.pooler.supabase.com/db", errors.BadUrl),
    ("mysql", SUPABASE, errors.BadUrl), ("redis", SUPABASE, errors.BadUrl),
    ("postgres", "postgresql://u:p@localhost/db", errors.HostNotAllowed),
    ("postgres", "postgresql://u:p@evil.example.com/db", errors.HostNotAllowed),
    ("postgres", "postgresql://u:p@a.b.pooler.supabase.com/db", errors.HostNotAllowed),
    ("postgres", "postgresql://u:p@127.0.0.1/db", errors.HostNotAllowed),
    ("postgres", "postgresql://u:p@169.254.169.254/db", errors.HostNotAllowed),
    ("postgres", "postgresql://u:p@[::1]:5432/db", errors.HostNotAllowed),
    ("postgres", "postgresql://u:p@aws-0-x.pooler.supabase.com:22/db", errors.HostNotAllowed),
    ("postgres", "postgresql://u:p@aws-0-x.pooler.supabase.com:5433/db", errors.HostNotAllowed),
    ("postgres", "postgresql://u:p@aws-0-x.pooler.supabase.com:abc/db", errors.BadUrl),
    ("postgres", "postgresql://u:p@aws-0-x.pooler.supabase.com:999999/db", errors.BadUrl),
    ("postgres", "postgresql://u:p@aws-0-x.pooler.supabase.com.:5432/db", errors.BadUrl),
    ("postgres", "postgresql://u:p@aws-0-x.pooler.supabase.com,aws-0-y.pooler.supabase.com/db", errors.BadUrl),
    ("postgres", "postgresql://:p@aws-0-x.pooler.supabase.com/db", errors.BadUrl),
    ("postgres", "postgresql://aws-0-x.pooler.supabase.com/db", errors.BadUrl),
    ("postgres", "postgresql://u:p@/db", errors.BadUrl),
    ("postgres", SUPABASE + "?sslmode=disable", errors.BadUrl), ("postgres", SUPABASE + "?sslmode=allow", errors.BadUrl),
    ("postgres", SUPABASE + "?host=10.0.0.1", errors.BadUrl), ("postgres", SUPABASE + "?hostaddr=10.0.0.1", errors.BadUrl),
    ("postgres", SUPABASE + "?options=-c%20statement_timeout%3D0", errors.BadUrl),
    ("postgres", SUPABASE + "?sslrootcert=/etc/x", errors.BadUrl), ("postgres", SUPABASE + "?passfile=x", errors.BadUrl),
    ("postgres", SUPABASE + "?sslmode=require&sslmode=require", errors.BadUrl),
    ("postgres", SUPABASE + "#frag", errors.BadUrl), ("postgres", SUPABASE.replace("s3cr3t-pass", "pa ss"), errors.BadUrl),
    ("postgres", SUPABASE.replace("s3cr3t-pass", "pa/ss"), errors.BadUrl),
    ("postgres", SUPABASE.replace("s3cr3t-pass", "%ff%fe"), errors.BadUrl),
    ("postgres", SUPABASE.replace("postgres.abcd", "u" * 300), errors.BadUrl),
    ("postgres", "postgresql://u:p@aws-0-x.pooler.supabase.com/a%2Fb", errors.BadUrl),
    ("mysql", "mysql://u:p@mysql-a.aivencloud.com/db?ssl-mode=DISABLED", errors.BadUrl),
    ("mysql", "mysql://u:p@mysql-a.aivencloud.com/db?ssl=false", errors.BadUrl),
    ("mysql", "mysql://u:p@mysql-a.aivencloud.com/db?local_infile=1", errors.BadUrl),
    ("mysql", "mysql://u:p@mysql-a.aivencloud.com:3307/db", errors.HostNotAllowed),
    ("mongodb", "mongodb+srv://u:p@cluster0.ab12c.mongodb.net/?tls=false", errors.BadUrl),
    ("mongodb", "mongodb+srv://u:p@cluster0.ab12c.mongodb.net/?ssl=false", errors.BadUrl),
    ("mongodb", "mongodb+srv://u:p@cluster0.ab12c.mongodb.net/?proxyHost=10.0.0.1", errors.BadUrl),
    ("mongodb", "mongodb+srv://u:p@cluster0.ab12c.mongodb.net/?authMechanism=MONGODB-AWS", errors.BadUrl),
    ("mongodb", "mongodb+srv://u:p@cluster0.ab12c.mongodb.net/?tlsAllowInvalidCertificates=true", errors.BadUrl),
    ("mongodb", "mongodb+srv://u:p@cluster0.ab12c.mongodb.net:27017/", errors.BadUrl),
    ("mongodb", "mongodb+srv://u:p@a.ab12c.mongodb.net,b.ab12c.mongodb.net/", errors.BadUrl),
    ("mongodb", "mongodb://u:p@" + ",".join(f"h{i}.ab12c.mongodb.net" for i in range(11)) + "/", errors.BadUrl),
    ("mongodb", "mongodb://u:p@h.ab12c.mongodb.net:27018/", errors.HostNotAllowed),
    ("mongodb", "mongodb://u:p@h.ab12c.mongodb.net,evil.example.com/", errors.HostNotAllowed),
    ("mongodb", "mongodb+srv://u:p@cluster0.ab12c.mongodb.net/?authSource=" + "x" * 200, errors.BadUrl),
])
def test_bad_urls_are_refused(engine, url, error):
    with pytest.raises(error) as caught:
        parse(engine, url)
    assert "s3cr3t-pass" not in caught.value.message and "pa/ss" not in caught.value.message


def test_resolve_pins_sql_to_the_first_checked_address():
    resolver = FakeResolver({"aws-0-ap-south-1.pooler.supabase.com": ["3.6.1.1", "3.6.1.2"]})
    target = run(validator(resolver).resolve(parse("postgres", SUPABASE)))
    assert (target.host, target.port, target.address, target.tls_verify) == (
        "aws-0-ap-south-1.pooler.supabase.com", 5432, "3.6.1.1", False)
    assert target.domain == "pooler.supabase.com" and "s3cr3t" not in repr(target)


@pytest.mark.parametrize("error", [errors.HostNotAllowed("x", "resolves to a private or reserved address"),
                                   errors.ConnectFailed("the host x could not be found")])
def test_resolve_passes_dns_refusals_through(error):
    with pytest.raises(type(error)):
        run(validator(FakeResolver(error=error)).resolve(parse("postgres", SUPABASE)))


def test_mongo_srv_hosts_must_be_allowed_and_public():
    good = FakeResolver(srv=[("ac-1-shard-00-00.ab12c.mongodb.net.", 27017), ("ac-1-shard-00-01.ab12c.mongodb.net", 27017)],
                        txt=["authSource=admin&replicaSet=atlas-x-shard-0&proxyHost=10.0.0.1"])
    target = run(validator(good).resolve(parse("mongodb", "mongodb+srv://u:p@cluster0.ab12c.mongodb.net/shop"
                                                          "?retryWrites=true&authSource=custom")))
    # The checked SRV hosts are what the driver gets: it never looks SRV up again.
    assert target.srv and target.address is None
    assert target.hosts == (("ac-1-shard-00-00.ab12c.mongodb.net", 27017), ("ac-1-shard-00-01.ab12c.mongodb.net", 27017))
    # TXT may only add authSource/replicaSet/loadBalanced, and URL options win.
    assert target.options == {"authSource": "custom", "replicaSet": "atlas-x-shard-0", "retryWrites": "true"}
    assert set(good.calls) == {"_mongodb._tcp.cluster0.ab12c.mongodb.net", ("txt", "cluster0.ab12c.mongodb.net"),
                               "ac-1-shard-00-00.ab12c.mongodb.net", "ac-1-shard-00-01.ab12c.mongodb.net"}


def test_dns_lookups_run_in_parallel_within_their_own_budget():
    import time as clock

    seeds = "mongodb://u:p@" + ",".join(f"h{i}.ab12c.mongodb.net:27017" for i in range(5)) + "/shop"
    started = clock.perf_counter()
    run(validator(FakeResolver(delay=0.2)).resolve(parse("mongodb", seeds)))
    assert clock.perf_counter() - started < 0.6   # five 0.2 s lookups at once, not one after another
    slow = TargetValidator(allowlist(), FakeResolver(delay=1.0), dns_timeout_seconds=0.1)
    with pytest.raises(errors.ConnectFailed) as caught:   # a slow DNS is a connection problem, not a slow query
        run(slow.resolve(parse("postgres", SUPABASE)))
    assert "timed out" in caught.value.message


def test_first_dns_failure_cancels_the_other_lookups():
    class OneBad(FakeResolver):
        async def resolve_public(self, host):
            if host.startswith("h0"):
                raise errors.HostNotAllowed(host, "resolves to a private or reserved address")
            await asyncio.sleep(5)
            return ["3.6.1.1"]

    seeds = "mongodb://u:p@" + ",".join(f"h{i}.ab12c.mongodb.net:27017" for i in range(3)) + "/shop"
    with pytest.raises(errors.HostNotAllowed):
        run(asyncio.wait_for(validator(OneBad()).resolve(parse("mongodb", seeds)), 2))
    for records in ([("evil.example.com", 27017)], [("ac-1.ab12c.mongodb.net", 27018)], [],
                    [("not a host!", 27017)]):
        with pytest.raises(errors.HostNotAllowed):
            run(validator(FakeResolver(srv=records)).resolve(
                parse("mongodb", "mongodb+srv://u:p@cluster0.ab12c.mongodb.net/shop")))


# ---- DNS resolver --------------------------------------------------------------------------


def resolver_with(*addresses):
    async def lookup(host):
        return list(addresses)

    return DnsHostResolver(lookup)


@pytest.mark.parametrize("addresses", [
    ("127.0.0.1",), ("10.0.0.5",), ("169.254.169.254",), ("192.168.1.10",), ("172.16.0.1",), ("100.64.0.1",),
    ("0.0.0.0",), ("::1",), ("fe80::1%eth0",), ("fc00::1",), ("::ffff:10.0.0.1",), ("224.0.0.1",),
    ("3.6.1.1", "10.0.0.5"),
])
def test_resolver_refuses_private_answers(addresses):
    with pytest.raises(errors.HostNotAllowed):
        run(resolver_with(*addresses).resolve_public("aws-0-x.pooler.supabase.com"))


def test_resolver_puts_ipv4_first_and_dedupes():
    found = run(resolver_with("2600:1f18::1", "3.6.1.1", "3.6.1.1").resolve_public("x"))
    assert found == ["3.6.1.1", "2600:1f18::1"]


def test_resolver_failures_are_connect_failed():
    async def broken(host):
        raise OSError("no route")

    async def empty(host):
        return []

    async def srv_broken(name):
        raise RuntimeError("dns exploded")

    for lookup in (broken, empty):
        with pytest.raises(errors.ConnectFailed):
            run(DnsHostResolver(lookup).resolve_public("x"))
    with pytest.raises(errors.ConnectFailed):
        run(DnsHostResolver(srv_lookup=srv_broken).resolve_srv("_mongodb._tcp.x"))


def test_real_lookup_of_localhost_is_refused():
    with pytest.raises(errors.HostNotAllowed):
        run(DnsHostResolver().resolve_public("localhost"))


def test_public_address_check():
    assert is_public_address("3.6.1.1") and is_public_address("2606:4700::1")
    assert not is_public_address("not-an-ip") and not is_public_address("")


# ---- JSON values and row caps -----------------------------------------------------------------


def test_values_convert_to_json():
    when = datetime(2026, 10, 25, 9, 30, tzinfo=timezone.utc)
    row = [Decimal("12.50"), when, date(2026, 1, 2), time(3, 4), timedelta(days=1, hours=2), b"\x00\xff",
           memoryview(b"ab"), uuid.UUID(int=1), float("nan"), float("inf"), {"k": [1, {"n": None}]}, {"b", "a"},
           ipaddress.ip_address("10.0.0.1"), (1, 2), True, 7, "x", None]
    assert to_json_value(row) == [
        "12.50", "2026-10-25T09:30:00+00:00", "2026-01-02", "03:04:00", "1 day, 2:00:00", "\\x00ff", "\\x6162",
        "00000000-0000-0000-0000-000000000001", "nan", "inf", {"k": [1, {"n": None}]}, ["a", "b"], "10.0.0.1",
        [1, 2], True, 7, "x", None]
    json.dumps(to_json_value(row), allow_nan=False)   # always valid JSON


def test_deeply_nested_values_are_flattened_to_text():
    value = current = []
    for _ in range(100):
        child = []
        current.append(child)
        current = child
    json.dumps(to_json_value(value))


def test_row_collector_caps():
    collector = RowCollector(max_rows=2, max_bytes=1000)
    assert collector.add([1]) and collector.add([2]) and not collector.add([3])
    assert collector.truncated and collector.rows == [[1], [2]]
    exact = RowCollector(max_rows=2, max_bytes=1000)
    assert exact.add([1]) and exact.add([2]) and not exact.truncated
    small = RowCollector(max_rows=100, max_bytes=20)
    assert small.add(["abc"]) and not small.add(["x" * 50]) and small.truncated
    error = collector.too_large({"rows": collector.rows})
    assert error.http_status == 413 and error.extra["truncated"] is True and error.extra["rows"] == [[1], [2]]


# ---- redaction ------------------------------------------------------------------------------


def test_redaction_removes_passwords_and_url_credentials():
    message = ("FATAL: password authentication failed (url postgresql://u:s3cr3t@pass@h/db, password=s3cr3t@pass, "
               "encoded s3cr3t%40pass, plus-encoded s3cr3t%40pass)")
    clean = redact(message, "s3cr3t@pass")
    assert "s3cr3t" not in clean and "postgresql://***@h/db" in clean
    assert "p@ss" not in redact("auth failed for p@ss and p%40ss", "p@ss")
    assert redact("abc", "a") == "abc"   # too short to redact safely
    assert len(redact("x" * 5000)) <= 501
    assert redact("\n".join(f"line {i}" for i in range(10))).count("\n") == 3


def test_credential_log_filter():
    record = logging.LogRecord("psycopg", logging.WARNING, __file__, 1, "connecting %s", ("mysql://u:pw@h/db",), None)
    assert CredentialLogFilter().filter(record) and record.getMessage() == "connecting mysql://***@h/db"
    other = logging.LogRecord("x", logging.INFO, __file__, 1, "password = hunter22 ok", None, None)
    CredentialLogFilter().filter(other)
    assert "hunter22" not in other.getMessage()
    try:
        raise RuntimeError("could not reach mongodb://learner:hunter22@x.mongodb.net/")
    except RuntimeError:
        import sys

        traced = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", None, sys.exc_info())
    CredentialLogFilter().filter(traced)
    assert "hunter22" not in logging.Formatter().format(traced)


def test_credential_filter_covers_child_loggers_through_the_root_handlers():
    import io

    from codelab_db.services.redaction import install_credential_log_filter

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        install_credential_log_filter()
        child = logging.getLogger("pymongo.connection")   # a child: logger-level filters never see it
        child.setLevel(logging.INFO)
        child.info("connecting mongodb://learner:hunter22@cluster0.x.mongodb.net/")
        assert "hunter22" not in stream.getvalue() and "mongodb://***@" in stream.getvalue()
    finally:
        root.removeHandler(handler)
        logging.getLogger("pymongo.connection").setLevel(logging.NOTSET)


# ---- limits ---------------------------------------------------------------------------------


def test_minute_limit_slides():
    limiter = MemoryLimiter(per_minute=2, per_day=100)
    limiter.consume(USER, NOW)
    limiter.consume(USER, NOW + timedelta(seconds=30))
    with pytest.raises(errors.RateLimited) as caught:
        limiter.consume(USER, NOW + timedelta(seconds=45))
    assert caught.value.extra == {"retry_after": 15, "scope": "minute"}
    assert caught.value.headers == {"Retry-After": "15"}
    limiter.consume(USER, NOW + timedelta(seconds=61))
    limiter.consume(uuid.uuid4(), NOW + timedelta(seconds=45))   # other learners unaffected


def test_day_limit_resets_at_utc_midnight():
    limiter = MemoryLimiter(per_minute=100, per_day=2)
    limiter.consume(USER, NOW)
    limiter.consume(USER, NOW)
    with pytest.raises(errors.RateLimited) as caught:
        limiter.consume(USER, NOW)
    assert caught.value.extra == {"retry_after": 12 * 3600, "scope": "day"}
    limiter.consume(USER, NOW + timedelta(hours=12))


def test_check_refuses_without_counting_and_consume_still_enforces():
    limiter = MemoryLimiter(per_minute=1, per_day=100)
    for _ in range(5):
        limiter.check(USER, NOW)   # never counts
    limiter.consume(USER, NOW)
    with pytest.raises(errors.RateLimited):
        limiter.check(USER, NOW)
    with pytest.raises(errors.RateLimited):   # two requests that both passed check: only one gets through
        limiter.consume(USER, NOW)
    assert limiter._day_counts[USER] == 1


def test_limiter_memory_is_swept():
    limiter = MemoryLimiter(per_minute=5, per_day=100)
    for _ in range(50):
        limiter.consume(uuid.uuid4(), NOW)
    limiter.consume(USER, NOW + timedelta(minutes=10))
    assert list(limiter._minute) == [USER]


# ---- request parsing -------------------------------------------------------------------------


def body(**fields):
    return json.dumps({"engine": "postgres", "url": SUPABASE, "sql": "SELECT 1", **fields}).encode()


def test_parse_valid_requests():
    request = parse_query_request(body(params=[1, "a", None, True, 1.5, [1, 2]]))
    assert isinstance(request, SqlQueryRequest) and request.params == [1, "a", None, True, 1.5, [1, 2]]
    assert parse_query_request(body(params={"a": 1})).params == {"a": 1}
    mongo_request = parse_query_request(json.dumps({"engine": "mongodb", "url": "mongodb+srv://h.x.mongodb.net",
                                                    "operation": "find", "collection": "c"}).encode())
    assert isinstance(mongo_request, MongoQueryRequest) and mongo_request.options.limit == 0
    assert parse_test_request(json.dumps({"engine": "mysql", "url": "mysql://u@h"}).encode()).engine == "mysql"
    assert "s3cr3t" not in repr(request) and "s3cr3t" not in str(request.model_dump())


@pytest.mark.parametrize("raw", [
    b"", b"not json", b"[1, 2]", b"\xff\xfe", b'{"engine": "redis", "url": "x", "sql": "1"}', b'{"url": "x"}',
    body(url=""), body(url="   "), body(url="x" * 3000), body(url=12), body(sql=""), body(sql=None),
    body(params=list(range(101))), body(params=[[[1]]]), body(params={"a": {"b": 1}}),
    json.dumps({"engine": "mongodb", "url": "x", "operation": "drop", "collection": "c"}).encode(),
    json.dumps({"engine": "mongodb", "url": "x", "operation": "find", "options": {"limit": -1}}).encode(),
    json.dumps({"engine": "mongodb", "url": "x", "operation": "find", "options": {"sort": [["a", 2]]}}).encode(),
    json.dumps({"engine": "mongodb", "url": "x", "operation": "insert_many", "documents": [{}] * 501}).encode(),
    json.dumps({"engine": "mongodb", "url": "x", "operation": "aggregate", "pipeline": [{}] * 51}).encode(),
    json.dumps({"engine": "mongodb", "url": "x", "operation": "find", "database": "d" * 65}).encode(),
])
def test_parse_refuses_bad_bodies(raw):
    with pytest.raises(errors.BadRequest) as caught:
        parse_query_request(raw)
    assert "s3cr3t" not in caught.value.message


def test_parse_refuses_deep_nesting_without_recursion_errors():
    deep = '{"engine": "mongodb", "url": "x", "operation": "find", "collection": "c", "filter": ' + \
           '{"a": ' * 5000 + "1" + "}" * 5000 + "}"
    with pytest.raises(errors.BadRequest):
        parse_query_request(deep.encode())
    nested = {"engine": "mongodb", "url": "x", "operation": "find", "collection": "c", "filter": {}}
    current = nested["filter"]
    for _ in range(70):
        current["a"] = {}
        current = current["a"]
    with pytest.raises(errors.BadRequest):
        parse_query_request(json.dumps(nested).encode())


def test_validation_errors_never_echo_the_url():
    with pytest.raises(errors.BadRequest) as caught:
        parse_query_request(body(sql=123))
    assert SUPABASE not in caught.value.message and "s3cr3t" not in caught.value.message


# ---- settings, executor -----------------------------------------------------------------------


def test_settings_defaults_and_environment(monkeypatch):
    defaults = DbProxySettings()
    assert (defaults.enabled, defaults.per_minute, defaults.per_day, defaults.statement_timeout_seconds,
            defaults.max_rows, defaults.max_response_bytes, defaults.max_sql_bytes) == (False, 60, 1000, 10, 500,
                                                                                        2097152, 20480)
    monkeypatch.setenv("DB_PROXY_ENABLED", "true")
    monkeypatch.setenv("DB_PROXY_BLOCKED_USER_IDS", f"{USER},junk")
    monkeypatch.setenv("DB_PROXY_CORS_ORIGINS", "https://a.example,*")
    configured = DbProxySettings()
    assert configured.enabled and configured.blocked_user_ids == {USER} and configured.cors_origin_list == [
        "https://a.example"]


def test_settings_ignore_the_dotenv_file(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("CORS_ORIGINS=*\nENABLED=true\nDB_PROXY_ENABLED=true\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert DbProxySettings().enabled is False


def test_invalid_settings_fall_back_to_switched_off(monkeypatch):
    from codelab_db import dependencies

    monkeypatch.setenv("DB_PROXY_MAX_ROWS", "0")
    monkeypatch.setenv("DB_PROXY_ENABLED", "true")
    fallback = dependencies._load_settings()
    assert fallback.enabled is False and fallback.max_rows == 500


def test_renewable_executor_rebuilds_after_shutdown():
    pool = RenewableExecutor(2)
    assert pool.submit(lambda: 1).result() == 1
    pool.shutdown(wait=True)
    assert pool.submit(lambda: 2).result() == 2
    pool.shutdown()
    pool.shutdown()


def test_executor_counts_busy_threads_until_they_really_finish():
    import threading

    release = threading.Event()
    pool = RenewableExecutor(2)
    first, second = pool.submit(release.wait, 5), pool.submit(release.wait, 5)
    assert pool.has_room() is False   # both threads busy, even if their requests already gave up
    release.set()
    first.result(), second.result()
    deadline = datetime.now() + timedelta(seconds=2)
    while not pool.has_room() and datetime.now() < deadline:
        pass
    assert pool.has_room() is True

    def boom():
        raise ValueError("x")

    with pytest.raises(ValueError):
        pool.submit(boom).result()
    assert pool.has_room() is True   # a failed task frees its slot too
    pool.shutdown()


# ---- code rules ---------------------------------------------------------------------------------


def sources():
    return sorted(PACKAGE_DIR.rglob("*.py"))


def test_no_staticmethod_or_classmethod():
    for source in sources():
        text = source.read_text(encoding="utf-8")
        assert "@staticmethod" not in text and "@classmethod" not in text, source


def _is_protocol(node: ast.ClassDef) -> bool:
    return any(getattr(base, "id", getattr(base, "attr", None)) == "Protocol" for base in node.bases)


def _is_plain_init(node) -> bool:
    if node.name != "__init__":
        return False
    for statement in node.body:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
            func = statement.value.func
            if isinstance(func, ast.Attribute) and func.attr == "__init__":
                continue
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
            continue
        return False
    return True


def test_every_function_body_is_wrapped_in_try():
    missing = []
    for source in sources():
        tree = ast.parse(source.read_text(encoding="utf-8"))
        protocol_methods = {id(item) for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and _is_protocol(node)
                            for item in node.body}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or id(node) in protocol_methods:
                continue
            if _is_plain_init(node):
                continue
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                body = body[1:]
            while body and isinstance(body[0], (ast.Assign, ast.AnnAssign)) and len(body) > 1:
                body = body[1:]
            if len(body) != 1 or not isinstance(body[0], ast.Try):
                missing.append(f"{source.name}:{node.lineno} {node.name}")
    assert missing == []


def test_drivers_are_imported_only_by_their_connector():
    owners = {"psycopg": {"pg_connector.py"}, "pymysql": {"mysql_connector.py"},
              "pymongo": {"mongo_connector.py"}, "bson": {"mongo_connector.py"}, "dns": {"dns_host_resolver.py"},
              "socket": {"dns_host_resolver.py", "mysql_connector.py"}}
    for source in sources():
        tree = ast.parse(source.read_text(encoding="utf-8"))
        imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                    for alias in node.names}
        imported |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                     and node.module}
        for library, files in owners.items():
            if library in imported:
                assert source.name in files, (library, source.name)


def test_the_host_grammar_and_prefix_live_in_constants():
    for source in sources():
        if source.name != "constants.py":
            text = source.read_text(encoding="utf-8")
            assert '"/api/codelab/db"' not in text and "[a-z0-9](?:[a-z0-9-]*[a-z0-9])?" not in text, source
