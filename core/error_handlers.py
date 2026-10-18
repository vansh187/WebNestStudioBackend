import json
import logging
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from sqlalchemy.exc import SQLAlchemyError

from core.exceptions import (
    BadRequestError,
    ConflictError,
    ContentRejectedError,
    DatabaseError,
    ExternalServiceError,
    ForbiddenError,
    GenerationUnavailableError,
    GitHubError,
    InvalidPromptError,
    NotFoundError,
    PayloadTooLargeError,
    RateLimitedError,
    UnauthorizedError,
    UnsupportedMediaTypeError,
    ValidationError,
)

from exams.errors import ExamDomainError
from exams.routing import ExamContractRoute

logger = logging.getLogger("webnest.errors")

def _is_exam_contract_route(request: Request) -> bool:
    """Routes declared with ExamContractRoute (exams/routing.py) use the
    course-exams 422 shape; FastAPI puts the matched route in the scope."""
    return isinstance(request.scope.get("route"), ExamContractRoute)


def _exam_validation_message(error: dict) -> str:
    location = ".".join(str(part) for part in error.get("loc", ()) if part not in ("body", "query", "path"))
    message = str(error.get("msg", "Invalid value")).removeprefix("Value error, ")
    return f"{location}: {message}" if location else message

_STATUS_BY_EXCEPTION = (
    (NotFoundError, 404),
    (BadRequestError, 400),
    (ConflictError, 409),
    (UnauthorizedError, 401),
    (ForbiddenError, 403),
    (InvalidPromptError, 400),
    (ValidationError, 422),
    (ContentRejectedError, 422),
    (PayloadTooLargeError, 413),
    (UnsupportedMediaTypeError, 415),
    (RateLimitedError, 429),
    (DatabaseError, 503),
    (ExternalServiceError, 503),
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

    @app.exception_handler(GitHubError)
    async def _github_exception_handler(request: Request, exc: GitHubError) -> JSONResponse:
        content: dict = {"detail": exc.message}
        if exc.code:
            content["code"] = exc.code
        content.update(exc.extra)
        headers = {}
        if "retry_after" in exc.extra:
            headers["Retry-After"] = str(exc.extra["retry_after"])
        return JSONResponse(status_code=exc.status_code, content=content, headers=headers)

    @app.exception_handler(ExamDomainError)
    async def _exam_exception_handler(request: Request, exc: ExamDomainError) -> JSONResponse:
        content: dict = {"detail": exc.message, "code": exc.code}
        for key, value in exc.extra.items():
            # Same UTC "Z" form as response models (cooldown_until etc.).
            if isinstance(value, datetime):
                value = value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            content[key] = value
        headers = {}
        if exc.extra.get("retry_after") is not None:
            headers["Retry-After"] = str(exc.extra["retry_after"])
        return JSONResponse(status_code=exc.status_code, content=jsonable_encoder(content), headers=headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(request: Request, exc: RequestValidationError) -> Response:
        if _is_exam_contract_route(request):
            # Never echo the input back (answers, names); detail is the first
            # problem as a plain string.
            raw_errors = list(exc.errors())
            errors = [
                {"loc": [str(part) for part in error.get("loc", ())], "msg": _exam_validation_message(error)}
                for error in raw_errors
            ]
            detail = errors[0]["msg"] if errors else "Request validation failed"
            content = {"detail": detail, "code": "validation_error", "errors": errors}
            try:
                return JSONResponse(status_code=422, content=content)
            except UnicodeEncodeError:
                # A lone UTF-16 surrogate in a location (e.g. a JSON key).
                return Response(
                    status_code=422, content=json.dumps(content, ensure_ascii=True), media_type="application/json"
                )
        if request.url.path.startswith("/api/github/"):
            # GitHub sync contract: {loc, msg} only - never echo the input,
            # which for a commit is whole file contents.
            errors = [
                {"loc": list(error.get("loc", ())), "msg": str(error.get("msg", "")).removeprefix("Value error, ")}
                for error in exc.errors()
            ]
            return JSONResponse(status_code=422, content={"detail": "Request validation failed", "errors": errors})
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
        content = {"detail": "Request validation failed", "errors": safe_errors}
        try:
            return JSONResponse(status_code=422, content=content)
        except UnicodeEncodeError:
            # The echoed input holds a lone UTF-16 surrogate (e.g. "\ud800"),
            # which UTF-8 cannot encode; ASCII-escaped JSON can.
            return Response(
                status_code=422,
                content=json.dumps(content, ensure_ascii=True),
                media_type="application/json",
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
