"""Blocked statements, functions and operations (B5.3), one guard per engine.

All checks run before any network call. The SQL guards work on the lexed SQL
(sql_lexer.py), so names inside comments never count and executable MySQL
comments and Postgres function bodies always do.

This is a usage policy, not the security boundary: the learner owns the
database and its credentials. What protects this server is the host
allowlist, the public-address check, TLS and the limits."""

import logging
import re

from codelab_db.constants import LOGGER_NAME, MONGO_SYSTEM_DATABASES, MONGO_WRITE_OPERATIONS
from codelab_db.errors import BadRequest, DbError, StatementBlocked
from codelab_db.protocols import MongoOperation, SqlStatement
from codelab_db.services.sql_lexer import LexedSql, lex

logger = logging.getLogger(LOGGER_NAME)

_SPACES = re.compile(r"\s+")
_WORDS = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\(|\)")

POSTGRES_KEYWORDS = (
    (re.compile(r"\bCOPY\b"), "COPY"),
    (re.compile(r"\bCREATE (OR REPLACE )?EXTENSION\b"), "CREATE EXTENSION"),
    (re.compile(r"\bALTER SYSTEM\b"), "ALTER SYSTEM"),
    (re.compile(r"\b(CREATE|ALTER) (ROLE|USER|GROUP)\b"), "CREATE/ALTER ROLE"),
    (re.compile(r"\bCREATE (SERVER|USER MAPPING|FOREIGN DATA WRAPPER|FOREIGN TABLE)\b"), "Foreign servers"),
    (re.compile(r"\bIMPORT FOREIGN SCHEMA\b"), "IMPORT FOREIGN SCHEMA"),
    (re.compile(r"\bLOAD '\?'"), "LOAD"),
    (re.compile(r"\bLANGUAGE (C|INTERNAL|PLPYTHON\w*|PLPERLU|PLTCLU|PLSH|PLV8)\b"), "Untrusted function languages"),
)
# DO blocks and function/procedure/language definitions: their bodies and
# language names may be written as ordinary quoted strings or identifiers
# ('...', "c"), and those run later, so the whole text is scanned for them.
_POSTGRES_ROUTINE = re.compile(r"^\s*(DO\b|CREATE (OR REPLACE )?((TRUSTED )?(PROCEDURAL )?LANGUAGE|FUNCTION|PROCEDURE)\b)")
POSTGRES_FUNCTIONS = re.compile(
    r"\b(pg_read_file|pg_read_binary_file|pg_ls_dir|pg_ls_[a-z_]*dir|pg_stat_file|lo_import|lo_export"
    r"|dblink[a-z_]*|pg_file_[a-z_]+|pg_logdir_ls) ?\(")

MYSQL_KEYWORDS = (
    (re.compile(r"\bLOAD (DATA|XML)\b"), "LOAD DATA"),
    (re.compile(r"\bINTO (OUTFILE|DUMPFILE)\b"), "SELECT ... INTO OUTFILE"),
    (re.compile(r"\bSET (GLOBAL|PERSIST|PERSIST_ONLY)\b"), "SET GLOBAL"),
    (re.compile(r"@@(GLOBAL|PERSIST|PERSIST_ONLY)\b"), "SET GLOBAL"),
    (re.compile(r"\bGRANT\b"), "GRANT"),
    (re.compile(r"\b(CREATE|ALTER|RENAME|DROP) USER\b"), "CREATE USER"),
    (re.compile(r"\bINSTALL (PLUGIN|COMPONENT)\b"), "INSTALL PLUGIN"),
    (re.compile(r"\bUNINSTALL\b"), "UNINSTALL PLUGIN"),
    (re.compile(r"\bCREATE SERVER\b"), "CREATE SERVER"),
    (re.compile(r"\bPREPARE\b"), "PREPARE (dynamic SQL)"),
    (re.compile(r"\bSHUTDOWN\b"), "SHUTDOWN"),
)
MYSQL_FUNCTIONS = re.compile(r"\b(load_file) ?\(")

_CURSOR_MAIN = {"SELECT", "VALUES", "TABLE"}
# Statements Postgres refuses inside a transaction block, and transaction
# control (one statement per request is already its own transaction).
_AUTOCOMMIT_FIRST = {"VACUUM", "CLUSTER", "REINDEX", "CHECKPOINT", "BEGIN", "START", "COMMIT", "END", "ROLLBACK",
                     "ABORT", "SAVEPOINT", "RELEASE", "DISCARD"}
_MAIN_WORDS = {"SELECT", "INSERT", "UPDATE", "DELETE", "MERGE", "VALUES", "TABLE"}
_DATA_MODIFYING = {"INSERT", "UPDATE", "DELETE", "MERGE"}

MONGO_BLOCKED_KEYS = {"$where": "$where", "$function": "$function", "$accumulator": "$accumulator",
                      "$out": "$out", "$merge": "$merge"}
_MONGO_BAD_DB_CHARS = set('/\\. "$*<>:|?\x00')


def _collapse(text: str) -> str:
    try:
        return _SPACES.sub(" ", text)
    except TypeError as exc:
        raise BadRequest("the SQL could not be read") from exc


def _without_separators(sql: str, lexed: LexedSql) -> str:
    """The single statement without its ; separators (a trailing ; breaks DECLARE)."""
    try:
        chars = list(sql)
        for position in lexed.semicolons:
            chars[position] = " "
        return "".join(chars).strip()
    except Exception as exc:
        raise BadRequest("the SQL could not be read") from exc


def _normal_params(params):
    try:
        if params is None or len(params) == 0:
            return None
        return dict(params) if isinstance(params, dict) else list(params)
    except Exception as exc:
        raise BadRequest("params could not be read") from exc


class _SqlGuard:
    """Shared steps; subclasses name their dialect, rules and statement kind."""

    dialect = ""
    keywords: tuple = ()
    functions: re.Pattern | None = None

    def __init__(self, max_sql_bytes: int) -> None:
        self._max_sql_bytes = max_sql_bytes

    def check(self, request, database_from_url: str | None) -> SqlStatement:
        try:
            sql = request.sql
            if len(sql.encode("utf-8")) > self._max_sql_bytes:
                raise BadRequest(f"the SQL is longer than {self._max_sql_bytes} bytes")
            if "\x00" in sql:
                raise BadRequest("the SQL contains a NUL character")
            lexed = lex(sql, self.dialect)
            if lexed.statements == 0:
                raise BadRequest("the SQL is empty")
            if lexed.statements > 1:
                raise StatementBlocked("More than one statement per request")
            code = self._keyword_view(lexed)
            for pattern, what in self.keywords:
                if pattern.search(code):
                    raise StatementBlocked(what)
            text = _collapse(lexed.text).lower()
            found = self.functions.search(text) if self.functions else None
            if found:
                raise StatementBlocked(f"{found.group(1)}()")
            kind, statement_type = self._classify(lexed.code)
            return SqlStatement(_without_separators(sql, lexed), _normal_params(request.params), kind, statement_type)
        except DbError:
            raise
        except Exception as exc:
            logger.warning("SQL guard failed dialect=%s type=%s", self.dialect, type(exc).__name__)
            raise BadRequest("the SQL could not be checked") from exc

    def _keyword_view(self, lexed: LexedSql) -> str:
        """What the keyword rules read: code plus $-bodies (strings elsewhere
        are data, so 'copy that' is not a COPY statement)."""
        try:
            return _collapse(lexed.code_bodies).upper()
        except DbError:
            raise
        except Exception as exc:
            raise BadRequest("the SQL could not be checked") from exc

    def _classify(self, code: str) -> tuple[str, str]:
        try:
            words = [word.upper() for word in _WORDS.findall(code)]
            first = words[0] if words else ""
            return "execute", (first if first.isalpha() else "SQL")[:16] or "SQL"
        except Exception as exc:
            raise BadRequest("the SQL could not be checked") from exc


class PostgresGuard(_SqlGuard):
    dialect = "postgres"
    keywords = POSTGRES_KEYWORDS
    functions = POSTGRES_FUNCTIONS

    def _keyword_view(self, lexed: LexedSql) -> str:
        """Routines (DO, CREATE FUNCTION/PROCEDURE/LANGUAGE) are scanned with
        every string and quoted name included, however the body is quoted."""
        try:
            code = _collapse(lexed.code_bodies).upper()
            if _POSTGRES_ROUTINE.match(_collapse(lexed.code).upper()):
                return code + " " + _collapse(lexed.text).upper()
            return code
        except DbError:
            raise
        except Exception as exc:
            raise BadRequest("the SQL could not be checked") from exc

    def _classify(self, code: str) -> tuple[str, str]:
        """"cursor" for plain SELECT/VALUES/TABLE (read through a server-side
        cursor, so a huge result never loads into memory); "autocommit" for
        statements that can't run in a transaction block (VACUUM, CREATE
        INDEX CONCURRENTLY, CREATE DATABASE, ...) and transaction control
        (BEGIN, COMMIT, ...); "execute" for everything else (writes, DDL,
        SHOW, EXPLAIN, RETURNING, SELECT INTO, data-modifying WITH)."""
        try:
            words = [word.upper() for word in _WORDS.findall(code)]
            if not words:
                return "execute", "SQL"
            first, second = words[0], (words[1] if len(words) > 1 else "")
            if (first in _AUTOCOMMIT_FIRST
                    or (first in ("CREATE", "DROP", "ALTER") and second in ("DATABASE", "TABLESPACE"))
                    or (first in ("CREATE", "DROP") and "CONCURRENTLY" in words)
                    or (first == "PREPARE" and second == "TRANSACTION")):
                return "autocommit", first[:16]
            depth, main, into_at_top = 0, None, False
            for word in words:
                if word == "(":
                    depth += 1
                elif word == ")":
                    depth = max(0, depth - 1)
                elif depth == 0:
                    if main is None and word in _MAIN_WORDS:
                        main = word
                    elif main == "SELECT" and word == "INTO":
                        into_at_top = True
            if words[0] == "(" and main is None:
                main = "SELECT"
            statement_type = words[0] if words[0] != "(" else "SELECT"
            modifying = any(word in _DATA_MODIFYING for word in words)
            if main in _CURSOR_MAIN and not modifying and not into_at_top and statement_type in ("SELECT", "WITH",
                                                                                                "VALUES", "TABLE"):
                return "cursor", statement_type
            return "execute", statement_type[:16]
        except Exception as exc:
            raise BadRequest("the SQL could not be checked") from exc


class MySqlGuard(_SqlGuard):
    dialect = "mysql"
    keywords = MYSQL_KEYWORDS
    functions = MYSQL_FUNCTIONS


def _check_mongo_name(value: str, what: str, max_length: int) -> None:
    try:
        if not value or len(value) > max_length:
            raise BadRequest(f"{what} must be 1 to {max_length} characters")
        if "\x00" in value:
            raise BadRequest(f"{what} contains a NUL character")
    except DbError:
        raise
    except Exception as exc:
        raise BadRequest(f"{what} could not be read") from exc


def _scan_mongo(value: object, where: str) -> None:
    """Refuses blocked operators and cross-database lookups anywhere in a
    filter, update, pipeline or document. Depth is already capped by the body
    parser; this walk is iterative anyway."""
    try:
        stack = [value]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                for key, child in current.items():
                    if isinstance(key, str) and key in MONGO_BLOCKED_KEYS:
                        raise StatementBlocked(f"{MONGO_BLOCKED_KEYS[key]} (in {where})")
                    stack.append(child)
                if "db" in current and "coll" in current:
                    raise StatementBlocked(f"Cross-database lookups (in {where})")
            elif isinstance(current, list):
                stack.extend(current)
    except DbError:
        raise
    except Exception as exc:
        raise BadRequest(f"{where} could not be checked") from exc


class MongoGuard:
    def check(self, request, database_from_url: str | None) -> MongoOperation:
        try:
            operation = request.operation
            database = request.database or database_from_url or ""
            if not database:
                raise BadRequest("database is required (in the request or the URL path)")
            _check_mongo_name(database, "database", 64)
            if any(char in _MONGO_BAD_DB_CHARS for char in database):
                raise BadRequest("database contains a character MongoDB doesn't allow")
            if database.lower() in MONGO_SYSTEM_DATABASES:
                raise StatementBlocked(f"The {database.lower()} database")
            collection = request.collection
            if operation != "list_collection_names":
                if not collection:
                    raise BadRequest("collection is required")
                _check_mongo_name(collection, "collection", 120)
                if "$" in collection or collection.startswith("system."):
                    raise StatementBlocked("System collections")
            if operation == "insert_one" and request.document is None:
                raise BadRequest("document is required for insert_one")
            if operation == "insert_many" and not request.documents:
                raise BadRequest("documents (a non-empty list) is required for insert_many")
            if operation in ("update_one", "update_many") and not request.update:
                raise BadRequest(f"update is required for {operation}")
            if operation == "aggregate" and request.pipeline is None:
                raise BadRequest("pipeline is required for aggregate")
            options = request.options
            for value, where in ((request.filter, "filter"), (request.update, "update"),
                                 (request.pipeline, "pipeline"), (options.projection, "projection"),
                                 (request.document, "document"), (request.documents, "documents")):
                if value is not None:
                    _scan_mongo(value, where)
            sort = options.sort
            if isinstance(sort, dict):
                sort = list(sort.items())
            return MongoOperation(
                operation=operation, database=database,
                collection=collection if operation != "list_collection_names" else None,
                filter=request.filter, update=request.update, document=request.document,
                documents=request.documents, pipeline=request.pipeline, projection=options.projection,
                sort=[[field, direction] for field, direction in sort] if sort else None,
                limit=options.limit, skip=options.skip,
                upsert=bool(options.upsert) and operation in ("update_one", "update_many"),
            )
        except DbError:
            raise
        except Exception as exc:
            logger.warning("Mongo guard failed type=%s", type(exc).__name__)
            raise BadRequest("the MongoDB request could not be checked") from exc


def mongo_statement_type(operation: str) -> str:
    try:
        return "WRITE" if operation in MONGO_WRITE_OPERATIONS else "READ"
    except TypeError:
        return "READ"
