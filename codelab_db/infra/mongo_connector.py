"""The only file importing pymongo / bson. One client per request, closed in
finally; TLS always. The client is built from the checked host list and an
allowlist of options - never from the learner's URI - so URI options such as
proxyHost or tls=false can't take effect.

For mongodb+srv the proxy has already read and checked the SRV (and TXT)
record, so pymongo gets the checked hosts as a seed list and never looks SRV
up again. A server_selector limits every operation - and so the credentials -
to exactly those hosts: a replica-set member the server announces that was
not checked is never used. (pymongo's background monitor may still open a TLS
connection to it to ask for its status, without credentials; those names
live in the provider's own DNS zone.)

Filters, updates, pipelines and documents may use MongoDB Extended JSON
($oid, $date, ...); results come back as relaxed Extended JSON."""

import asyncio
import json
import logging
from concurrent.futures import Executor
from functools import partial

import pymongo
from bson import json_util
from bson.errors import BSONError
from bson.json_util import RELAXED_JSON_OPTIONS
from pymongo import errors as mongo_errors

from codelab_db.constants import LOGGER_NAME
from codelab_db.errors import BadRequest, ConnectFailed, DbError, QueryError, QueryTimeout
from codelab_db.protocols import Limits, MongoOperation, MongoResult, ServerInfo, Target
from codelab_db.services.json_values import RowCollector
from codelab_db.services.redaction import redact

logger = logging.getLogger(LOGGER_NAME)

APP_NAME = "webnest-codelab"
AUTH_FAILED_CODES = (18, 8000)   # AuthenticationFailed, Atlas "bad auth"
_BOOL_OPTIONS = ("retryWrites", "retryReads", "loadBalanced")


def checked_servers(allowed: frozenset, servers: list) -> list:
    """pymongo server_selector: only servers whose (host, port) was checked."""
    try:
        return [server for server in servers
                if (str(server.address[0]).lower(), int(server.address[1])) in allowed]
    except Exception:
        logger.warning("mongo server selection filter failed; no server selected")
        return []


def _load(value):
    """Request JSON → BSON-ready Python ($oid → ObjectId, $date → datetime)."""
    try:
        if value is None:
            return None
        return json_util.loads(json.dumps(value))
    except (ValueError, TypeError, BSONError) as exc:
        raise BadRequest(f"a value is not valid MongoDB Extended JSON ({type(exc).__name__})") from exc
    except Exception as exc:
        raise BadRequest("a value could not be read as MongoDB Extended JSON") from exc


def _dump(value):
    """BSON value → plain JSON in relaxed Extended JSON form."""
    try:
        return json.loads(json_util.dumps(value, json_options=RELAXED_JSON_OPTIONS))
    except Exception as exc:
        logger.warning("a MongoDB value could not be converted type=%s", type(value).__name__)
        raise QueryError("A value in the result could not be converted to JSON.") from exc


def _client_options(target: Target, limits: Limits) -> dict:
    try:
        kwargs = dict(
            tls=True, appname=APP_NAME, maxPoolSize=1, minPoolSize=0, connect=True, tz_aware=True,
            serverSelectionTimeoutMS=limits.connect_timeout_seconds * 1000,
            connectTimeoutMS=limits.connect_timeout_seconds * 1000,
            socketTimeoutMS=(limits.statement_timeout_seconds + 2) * 1000,
            uuidRepresentation="standard",
        )
        if not target.tls_verify:
            kwargs["tlsAllowInvalidCertificates"] = True
        elif limits.ca_file:
            kwargs["tlsCAFile"] = limits.ca_file
        if target.user:
            kwargs["username"] = target.user
            kwargs["password"] = target.password or ""
        allowed = frozenset((host.lower(), int(port or 27017)) for host, port in target.hosts)
        kwargs["server_selector"] = partial(checked_servers, allowed)
        for key, value in target.options.items():
            if key in _BOOL_OPTIONS:
                kwargs[key] = value.lower() in ("true", "1")
            elif key == "w":
                kwargs[key] = int(value) if value.isdigit() else value
            else:
                kwargs[key] = value
        return kwargs
    except Exception as exc:
        raise BadRequest("the MongoDB URL options could not be read") from exc


class MongoConnector:
    def __init__(self, executor: Executor, client_class=None) -> None:
        self._executor = executor
        self._client_class = client_class or pymongo.MongoClient

    async def run(self, target: Target, request: MongoOperation, limits: Limits) -> MongoResult:
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self._executor, partial(self._run_sync, target, request, limits))
        except DbError:
            raise
        except Exception as exc:
            logger.error("mongo run failed domain=%s type=%s", target.domain, type(exc).__name__)
            raise QueryError("The operation could not be run.") from exc

    async def test(self, target: Target, limits: Limits) -> ServerInfo:
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self._executor, partial(self._test_sync, target, limits))
        except DbError:
            raise
        except Exception as exc:
            logger.error("mongo test failed domain=%s type=%s", target.domain, type(exc).__name__)
            raise ConnectFailed("the connection test could not be completed") from exc

    def _client(self, target: Target, limits: Limits):
        try:
            hosts = [f"{host}:{port or 27017}" for host, port in target.hosts]
            return self._client_class(hosts, **_client_options(target, limits))
        except DbError:
            raise
        except mongo_errors.PyMongoError as exc:
            raise ConnectFailed(redact(str(exc), target.password)) from exc
        except Exception as exc:
            logger.warning("mongo client failed domain=%s type=%s", target.domain, type(exc).__name__)
            raise ConnectFailed("the database client could not be created") from exc

    def _run_sync(self, target: Target, operation: MongoOperation, limits: Limits) -> MongoResult:
        client = self._client(target, limits)
        try:
            with pymongo.timeout(limits.statement_timeout_seconds):
                database = client.get_database(operation.database)
                if operation.operation == "list_collection_names":
                    return self._collect(sorted(database.list_collection_names()), target, limits, plain=True)
                return self._operate(database.get_collection(operation.collection), operation, target, limits)
        except DbError:
            raise
        except Exception as exc:
            raise self._map(exc, target, limits) from exc
        finally:
            _close(client)

    def _operate(self, collection, op: MongoOperation, target: Target, limits: Limits) -> MongoResult:
        try:
            ms = limits.statement_timeout_seconds * 1000
            query = _load(op.filter) or {}
            name = op.operation
            if name in ("find", "find_one"):
                limit = 1 if name == "find_one" else (
                    limits.max_rows + 1 if op.limit == 0 or op.limit > limits.max_rows else op.limit)
                cursor = collection.find(query, projection=_load(op.projection), skip=op.skip, limit=limit,
                                         sort=[(field, direction) for field, direction in op.sort] if op.sort else None,
                                         max_time_ms=ms)
                return self._collect(cursor, target, limits)
            if name == "count_documents":
                kwargs = {"maxTimeMS": ms}
                if op.skip:
                    kwargs["skip"] = op.skip
                if op.limit:
                    kwargs["limit"] = op.limit
                return MongoResult([], int(collection.count_documents(query, **kwargs)), None)
            if name == "aggregate":
                cursor = collection.aggregate(_load(op.pipeline) or [], maxTimeMS=ms, allowDiskUse=False,
                                              batchSize=min(limits.max_rows + 1, 1000))
                return self._collect(cursor, target, limits)
            if name == "insert_one":
                inserted = collection.insert_one(_load(op.document))
                return MongoResult([], 1, {"inserted_id": _dump(inserted.inserted_id)})
            if name == "insert_many":
                inserted = collection.insert_many(_load(op.documents), ordered=True)
                ids = [_dump(value) for value in inserted.inserted_ids]
                return MongoResult([], len(ids), {"inserted_ids": ids})
            if name in ("update_one", "update_many"):
                method = collection.update_one if name == "update_one" else collection.update_many
                updated = method(query, _load(op.update), upsert=op.upsert)
                return MongoResult([], int(updated.modified_count), {
                    "matched_count": int(updated.matched_count), "modified_count": int(updated.modified_count),
                    "upserted_id": _dump(updated.upserted_id) if updated.upserted_id is not None else None})
            if name in ("delete_one", "delete_many"):
                method = collection.delete_one if name == "delete_one" else collection.delete_many
                deleted = method(query)
                return MongoResult([], int(deleted.deleted_count), {"deleted_count": int(deleted.deleted_count)})
            raise BadRequest(f"unknown operation {name!r}")
        except DbError:
            raise
        except Exception as exc:
            raise self._map(exc, target, limits) from exc

    def _collect(self, items, target: Target, limits: Limits, plain: bool = False) -> MongoResult:
        try:
            collector = RowCollector(limits.max_rows, limits.max_bytes)
            for item in items:
                if not collector.add(item if plain else _dump(item)):
                    raise collector.too_large({"documents": collector.rows, "count": len(collector.rows)})
            return MongoResult(collector.rows, len(collector.rows), None)
        except DbError:
            raise
        except Exception as exc:
            raise self._map(exc, target, limits) from exc

    def _map(self, exc: Exception, target: Target, limits: Limits) -> DbError:
        try:
            if isinstance(exc, mongo_errors.ServerSelectionTimeoutError):
                return ConnectFailed(redact(_short(exc), target.password))
            if isinstance(exc, mongo_errors.PyMongoError) and getattr(exc, "timeout", False):
                return QueryTimeout(limits.statement_timeout_seconds)
            if isinstance(exc, mongo_errors.OperationFailure) and exc.code in AUTH_FAILED_CODES:
                return ConnectFailed(redact(_short(exc), target.password))
            if isinstance(exc, (mongo_errors.ConnectionFailure, mongo_errors.ConfigurationError,
                                mongo_errors.InvalidURI)):
                return ConnectFailed(redact(_short(exc), target.password))
            if isinstance(exc, (mongo_errors.PyMongoError, BSONError)):
                return QueryError(redact(_short(exc), target.password))
            if isinstance(exc, (OverflowError, ValueError, TypeError)):
                return QueryError(f"A value isn't supported by MongoDB ({type(exc).__name__}).")
            logger.warning("mongo operation failed domain=%s type=%s", target.domain, type(exc).__name__)
            return QueryError("The operation could not be run.")
        except Exception:
            return QueryError("The operation could not be run.")

    def _test_sync(self, target: Target, limits: Limits) -> ServerInfo:
        client = self._client(target, limits)
        try:
            with pymongo.timeout(limits.connect_timeout_seconds + limits.statement_timeout_seconds):
                return ServerInfo(str(client.server_info().get("version", "unknown")))
        except DbError:
            raise
        except Exception as exc:
            mapped = self._map(exc, target, limits)
            raise (mapped if isinstance(mapped, ConnectFailed) else ConnectFailed(mapped.message)) from exc
        finally:
            _close(client)


def _short(error: Exception) -> str:
    """pymongo errors can carry the full server topology; keep the first part."""
    try:
        text = str(error)
        return text.split(", Timeout:")[0].split(", full error:")[0] or type(error).__name__
    except Exception:
        return type(error).__name__


def _close(client) -> None:
    try:
        client.close()
    except Exception as exc:
        logger.debug("mongo client close failed type=%s", type(exc).__name__)
