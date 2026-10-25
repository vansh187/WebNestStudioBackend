"""HTTP only: read the body, call the service, map domain errors to HTTP."""

import logging

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from codelab_db.constants import DB_PATH, LOGGER_NAME
from codelab_db.dependencies import check_db_enabled, describe_proxy, get_body_reader, get_db_user, get_query_service
from codelab_db.errors import DbError, QueryError
from codelab_db.protocols import DbUser
from codelab_db.schemas import parse_query_request, parse_test_request
from codelab_db.services.http_io import BodyReader
from codelab_db.services.query_service import QueryService

logger = logging.getLogger(LOGGER_NAME)

router = APIRouter(prefix=DB_PATH, tags=["codelab-db"])

SECURITY_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def db_error_response(error: DbError) -> JSONResponse:
    try:
        headers = {**error.headers, **SECURITY_HEADERS, "X-WebNest-DB-Error": error.code}
        return JSONResponse(status_code=error.http_status,
                            content={"detail": error.message, "code": error.code, **error.extra}, headers=headers)
    except Exception as exc:
        logger.error("could not render database proxy error code=%s type=%s", getattr(error, "code", "?"),
                     type(exc).__name__)
        return JSONResponse(status_code=422, content={"detail": "The query could not be run.",
                                                      "code": "db_query_error"},
                            headers={**SECURITY_HEADERS, "X-WebNest-DB-Error": "db_query_error"})


async def _db_error_handler(request: Request, exc: Exception) -> JSONResponse:
    try:
        return db_error_response(exc if isinstance(exc, DbError) else QueryError("The query could not be run."))
    except Exception as error:
        logger.error("database proxy error handler failed type=%s", type(error).__name__)
        raise


def register_db_error_handlers(app: FastAPI) -> None:
    try:
        app.add_exception_handler(DbError, _db_error_handler)
    except Exception:
        logger.critical("could not register the database proxy error handler", exc_info=True)
        raise


@router.get("/status")
async def status() -> JSONResponse:
    """Public: whether the proxy is on, the supported hosts and the limits."""
    try:
        return JSONResponse(describe_proxy(), headers={"Cache-Control": "public, max-age=300",
                                                       "X-Content-Type-Options": "nosniff"})
    except DbError:
        raise
    except Exception as exc:
        logger.exception("unexpected error in database proxy status")
        raise QueryError("The status could not be read.") from exc


@router.post("/query")
async def query(request: Request, _enabled: None = Depends(check_db_enabled), user: DbUser = Depends(get_db_user),
                service: QueryService = Depends(get_query_service),
                reader: BodyReader = Depends(get_body_reader)) -> JSONResponse:
    try:
        body = parse_query_request(await reader.read(request))
        return JSONResponse(await service.query(user.id, body), headers=SECURITY_HEADERS)
    except DbError:
        raise
    except Exception as exc:
        logger.error("unexpected error in database proxy query type=%s", type(exc).__name__)
        raise QueryError("The query could not be run.") from exc


@router.post("/test")
async def test_connection(request: Request, _enabled: None = Depends(check_db_enabled),
                          user: DbUser = Depends(get_db_user), service: QueryService = Depends(get_query_service),
                          reader: BodyReader = Depends(get_body_reader)) -> JSONResponse:
    try:
        body = parse_test_request(await reader.read(request))
        return JSONResponse(await service.test(user.id, body), headers=SECURITY_HEADERS)
    except DbError:
        raise
    except Exception as exc:
        logger.error("unexpected error in database proxy connection test type=%s", type(exc).__name__)
        raise QueryError("The connection could not be tested.") from exc
