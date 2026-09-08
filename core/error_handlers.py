import logging

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from core.exceptions import (
    ConflictError,
    ContentRejectedError,
    DatabaseError,
    ForbiddenError,
    GenerationUnavailableError,
    InvalidPromptError,
    NotFoundError,
    PayloadTooLargeError,
    RateLimitedError,
    UnauthorizedError,
    ValidationError,
)

logger = logging.getLogger("webnest.errors")

_STATUS_BY_EXCEPTION = (
    (NotFoundError, 404),
    (ConflictError, 409),
    (UnauthorizedError, 401),
    (ForbiddenError, 403),
    (InvalidPromptError, 400),
    (ValidationError, 422),
    (ContentRejectedError, 422),
    (PayloadTooLargeError, 413),
    (RateLimitedError, 429),
    (DatabaseError, 503),
    (GenerationUnavailableError, 503),
)


def register_error_handlers(app: FastAPI) -> None:
    """Registers every exception handler so no unhandled exception ever reaches the client as a raw 500."""

    for exception_type, status_code in _STATUS_BY_EXCEPTION:

        def _make_domain_handler(code: int):
            async def _handler(request: Request, exc: Exception) -> JSONResponse:
                return JSONResponse(status_code=code, content={"detail": str(exc)})

            return _handler

        app.add_exception_handler(exception_type, _make_domain_handler(status_code))

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        # exc.errors() may embed a raw exception object under ctx.error (from a
        # raised ValueError inside a validator) which json.dumps cannot serialize.
        safe_errors = []
        for error in exc.errors():
            error = dict(error)
            ctx = error.get("ctx")
            if isinstance(ctx, dict) and "error" in ctx:
                ctx = {**ctx, "error": str(ctx["error"])}
                error["ctx"] = ctx
            safe_errors.append(error)
        safe_errors = jsonable_encoder(safe_errors)
        return JSONResponse(
            status_code=422,
            content={"detail": "Request validation failed", "errors": safe_errors},
        )

    @app.exception_handler(SQLAlchemyError)
    async def _database_exception_handler(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        logger.error("Unhandled database error on %s %s", request.method, request.url.path, exc_info=True)
        return JSONResponse(
            status_code=503,
            content={"detail": "A database error occurred. Please try again in a moment."},
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled exception on %s %s", request.method, request.url.path, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"detail": "An unexpected error occurred. Please try again later."},
        )
