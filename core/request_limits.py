import json
import logging

from fastapi import HTTPException, Request, status
from starlette.requests import ClientDisconnect

from core.exceptions import PayloadTooLargeError

logger = logging.getLogger("webnest.request_limits")

# 2 MB of commit content can grow to several times that once JSON-escaped.
GITHUB_MAX_BODY_BYTES = 16 * 1024 * 1024
_LIMITED_METHODS = ("POST", "PUT", "PATCH")
TOO_LARGE = "This request is too large."


class GitHubBodyLimitMiddleware:
    """Rejects an oversized request to /api/github/* with the contract's 413
    before the route parses it, so one request can't make the server buffer
    an arbitrarily large payload.

    A declared Content-Length is checked up front. A body without one
    (chunked transfer) is read here chunk by chunk and stopped as soon as it
    passes the limit; a body within the limit is handed on unchanged."""

    def __init__(self, app, max_bytes: int = GITHUB_MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        if not (
            scope.get("type") == "http"
            and str(scope.get("path", "")).startswith("/api/github/")
            and scope.get("method") in _LIMITED_METHODS
        ):
            await self.app(scope, receive, send)
            return

        length = dict(scope.get("headers") or []).get(b"content-length")
        if length is not None:
            if not length.isdigit() or int(length) > self.max_bytes:
                await self._reject(send)
                return
            await self.app(scope, receive, send)
            return

        # No Content-Length: buffer up to the limit, then replay the body.
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                return
            chunk = message.get("body", b"") or b""
            size += len(chunk)
            if size > self.max_bytes:
                await self._reject(send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break

        body = b"".join(chunks)
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    async def _reject(self, send) -> None:
        body = json.dumps({"detail": "This request is too large for GitHub sync.", "code": "payload_too_large"}).encode()
        try:
            await send(
                {
                    "type": "http.response.start",
                    "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
                }
            )
            await send({"type": "http.response.body", "body": body})
        except Exception:
            # The client already went away; nothing left to tell it.
            logger.debug("Could not send the 413 response", exc_info=True)


async def read_limited_body(request: Request, limit: int, too_large: str = TOO_LARGE) -> bytes:
    """A route's raw body, untouched so a signature over it can still be checked. FastAPI would
    read a declared body whole before any check; this refuses a declared size over the limit
    before reading anything, and otherwise stops as soon as the bytes pass it, so a chunked
    body can't get round the limit."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise PayloadTooLargeError(too_large)
    chunks: list[bytes] = []
    size = 0
    try:
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                raise PayloadTooLargeError(too_large)
            chunks.append(chunk)
    except ClientDisconnect as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The request was not completed.") from exc
    return b"".join(chunks)
