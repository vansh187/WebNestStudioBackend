"""HTTP plumbing for WebNest AI in VS Code (/api/extension/*): the request id, the coded error
body and the CORS policies. Every non-2xx answer under the prefix is
{"detail", "code", "retry_after"?, "request_id"}, so the extension switches on `code`, never on
the English text, and nothing here echoes request input back."""

import json
import logging
import re
import uuid
from typing import Any
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("webnest.extension")

EXTENSION_PREFIX = "/api/extension"
# Called by the website in a browser, with the website's own token; nothing else here is.
EXTENSION_CODES_PATH = EXTENSION_PREFIX + "/auth/codes"
EXTENSION_SESSIONS_PATH = EXTENSION_PREFIX + "/sessions"
REQUEST_ID_HEADER = "X-Request-Id"
_STATE_KEY = "extension_request_id"
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

_CODE_BY_STATUS: dict[int, str] = {
    400: "bad_request",
    401: "not_authenticated",
    402: "prime_required",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_error",
    426: "extension_outdated",
    429: "rate_limited",
    500: "internal_error",
    502: "service_unavailable",
    503: "service_unavailable",
    504: "service_unavailable",
}


def is_extension_path(path: str) -> bool:
    """The prefix itself and anything below it, not look-alikes such as /api/extensions."""
    return path == EXTENSION_PREFIX or path.startswith(EXTENSION_PREFIX + "/")


def code_for_status(status_code: int) -> str:
    known = _CODE_BY_STATUS.get(status_code)
    if known is not None:
        return known
    return "internal_error" if status_code >= 500 else "bad_request"


def _chosen_request_id(supplied: str | None) -> str:
    """The caller's id when it is a sane one, else a new one: it is echoed in a header and
    written to logs, so it must never carry arbitrary text."""
    if supplied and _REQUEST_ID.match(supplied):
        return supplied
    return str(uuid.uuid4())


def request_id(request: Request) -> str:
    """This request's id, fixed on first use so the header and the body always agree."""
    state = request.scope.setdefault("state", {})
    known = state.get(_STATE_KEY)
    if isinstance(known, str):
        return known
    chosen = _chosen_request_id(request.headers.get(REQUEST_ID_HEADER))
    state[_STATE_KEY] = chosen
    return chosen


def extension_error_response(
    request: Request,
    status_code: int,
    code: str,
    detail: str,
    retry_after: int | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> Response:
    rid = request_id(request)
    body: dict[str, Any] = {"detail": detail, "code": code}
    headers: dict[str, str] = {REQUEST_ID_HEADER: rid}
    if errors is not None:
        body["errors"] = errors
    if retry_after is not None:
        seconds = max(int(retry_after), 1)
        body["retry_after"] = seconds
        headers["Retry-After"] = str(seconds)
    body["request_id"] = rid
    # ensure_ascii: a lone surrogate in a message can never break the encoding of an error.
    content = json.dumps(body, ensure_ascii=True, default=str)
    return Response(status_code=status_code, content=content, media_type="application/json", headers=headers)


def validation_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """{loc, msg} only, never the input, which may be the user's code."""
    cleaned: list[dict[str, Any]] = []
    for error in errors:
        try:
            location = [part if isinstance(part, int) else str(part) for part in error.get("loc", ())]
            message = str(error.get("msg", "Invalid value")).removeprefix("Value error, ")
        except Exception:
            location, message = [], "Invalid value"
        cleaned.append({"loc": location, "msg": message})
    return cleaned


def retry_after_from(value: Any) -> int | None:
    """A Retry-After header value as whole seconds, or None when it isn't a number."""
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


class ExtensionRequestIdMiddleware:
    """Gives every /api/extension/* response an X-Request-Id header: the caller's id when it
    sent a sane one, else a new one. Other paths pass straight through."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or not is_extension_path(str(scope.get("path", ""))):
            await self.app(scope, receive, send)
            return
        rid = request_id(Request(scope))
        header_name = REQUEST_ID_HEADER.lower().encode("latin-1")
        header_value = rid.encode("latin-1")

        async def send_with_id(message: Message) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                if not any(name.lower() == header_name for name, _ in headers):
                    headers.append((header_name, header_value))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_id)


def _origin_of(url: str) -> str | None:
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


def website_origins(frontend_base_url: str, cors_origins: list[str]) -> list[str]:
    """The website, with and without www (one redirects to the other), plus any origin listed
    explicitly in CORS_ORIGINS, such as a local frontend. A wildcard is never one of them."""
    origins: list[str] = []
    main = _origin_of(frontend_base_url)
    if main is not None:
        origins.append(main)
        scheme, host = main.split("://", 1)
        twin = host[4:] if host.startswith("www.") else f"www.{host}"
        origins.append(f"{scheme}://{twin}")
    for origin in cors_origins:
        cleaned = _origin_of(origin)
        if origin.strip() != "*" and cleaned is not None:
            origins.append(cleaned)
    return list(dict.fromkeys(origins))


def extension_cors_policies(frontend_base_url: str, cors_origins: list[str]) -> tuple[tuple[str, dict[str, Any]], ...]:
    """(prefix, CORSMiddleware options), most specific first. Only the two routes the website
    calls from a browser get its origin; the rest of the extension API isn't for browsers,
    so it sends no CORS headers at all and a cross-site page can't read it."""
    website: dict[str, Any] = {
        "allow_origins": website_origins(frontend_base_url, cors_origins),
        "allow_credentials": True,
        "allow_methods": ["GET", "POST", "DELETE", "OPTIONS"],
        "allow_headers": ["*"],
        "expose_headers": ["Retry-After", REQUEST_ID_HEADER],
        "max_age": 600,
    }
    closed: dict[str, Any] = {"allow_origins": [], "allow_methods": [], "allow_headers": []}
    return (
        (EXTENSION_CODES_PATH, website),
        (EXTENSION_SESSIONS_PATH, website),
        (EXTENSION_PREFIX, closed),
    )
