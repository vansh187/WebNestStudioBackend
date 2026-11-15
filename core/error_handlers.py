import json
import logging
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from core.exceptions import (
    BadRequestError,
    ConflictError,
    ContentRejectedError,
    DatabaseError,
    ExtensionError,
    ExternalServiceError,
    ForbiddenError,
    GenerationUnavailableError,
    GitHubError,
    InvalidPromptError,
    NotFoundError,
    PayloadTooLargeError,
    PaymentRequiredError,
    RateLimitedError,
    UnauthorizedError,
    UnsupportedMediaTypeError,
    ValidationError,
)

from core.extension_http import (
    code_for_status,
    extension_error_response,
    is_extension_path,
    retry_after_from,
    validation_errors,
)
from exams.errors import ExamDomainError
from exams.routing import ExamContractRoute

logger = logging.getLogger("webnest.errors")

# Shown for a database or unexpected failure on the VS Code extension's routes. Never the
# exception's own text, which could hold a query or a value.
_EXTENSION_DATABASE_MESSAGE = "WebNest AI can't reach its records right now. Please try again in a moment."
_EXTENSION_DATABASE_RETRY_SECONDS = 5
_EXTENSION_INTERNAL_MESSAGE = "An unexpected error occurred. Please try again later."

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
    (PaymentRequiredError, 402),
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
            async def _handler(request: Request, exc: Exception) -> Response:
                if is_extension_path(request.url.path):
                    return _extension_domain_response(request, code, exc)
                return JSONResponse(status_code=code, content={"detail": str(exc)})

            return _handler

        app.add_exception_handler(exception_type, _make_domain_handler(status_code))

    @app.exception_handler(ExtensionError)
    async def _extension_exception_handler(request: Request, exc: ExtensionError) -> Response:
        return extension_error_response(request, exc.status_code, exc.code, exc.message, exc.retry_after, exc.errors)

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> Response:
        """Unchanged everywhere else; under /api/extension/ it gets the coded shape, which also
        covers FastAPI's own 404 and 405 there."""
        if not is_extension_path(request.url.path):
            return await http_exception_handler(request, exc)
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed"
        retry_after = retry_after_from(exc.headers.get("Retry-After")) if exc.headers else None
        return extension_error_response(request, exc.status_code, code_for_status(exc.status_code), detail, retry_after)

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
        if is_extension_path(request.url.path):
            return extension_error_response(
                request, 422, "validation_error", "Request validation failed", errors=validation_errors(list(exc.errors()))
            )
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
    async def _database_exception_handler(request: Request, exc: SQLAlchemyError) -> Response:
        logger.error("Unhandled database error on %s %s", request.method, request.url.path, exc_info=True)
        if is_extension_path(request.url.path):
            return extension_error_response(
                request, 503, "service_unavailable", _EXTENSION_DATABASE_MESSAGE, _EXTENSION_DATABASE_RETRY_SECONDS
            )
        return JSONResponse(
            status_code=503,
            content={"detail": "A database error occurred. Please try again in a moment."},
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception) -> Response:
        logger.error("Unhandled exception on %s %s", request.method, request.url.path, exc_info=True)
        if is_extension_path(request.url.path):
            return extension_error_response(request, 500, "internal_error", _EXTENSION_INTERNAL_MESSAGE)
        return JSONResponse(
            status_code=500,
            content={"detail": "An unexpected error occurred. Please try again later."},
        )


def _extension_domain_response(request: Request, status_code: int, exc: Exception) -> Response:
    """A service-layer error on an extension route: its status, the code for that status,
    and any wait it carries. A database failure shows a fixed message, never its own text."""
    if status_code == 503 and isinstance(exc, DatabaseError):
        return extension_error_response(
            request, 503, "service_unavailable", _EXTENSION_DATABASE_MESSAGE, _EXTENSION_DATABASE_RETRY_SECONDS
        )
    code = getattr(exc, "code", None)
    retry_after = retry_after_from(getattr(exc, "retry_after", None))
    return extension_error_response(
        request,
        status_code,
        code if isinstance(code, str) and code else code_for_status(status_code),
        str(exc) or "Request failed",
        retry_after,
    )
