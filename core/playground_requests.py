"""Request helpers shared by the CodeLab relay and database proxy: reading a
size-capped body, and the WebNest login sent in X-WebNest-Authorization.

Each feature passes its own error base class and factories, so responses
keep that feature's error contract (relay_* / db_* codes)."""

import logging
import uuid
from collections.abc import Callable

from fastapi import Request
from starlette.requests import ClientDisconnect

from core.exceptions import UnauthorizedError

logger = logging.getLogger("webnest.playground")


async def read_capped_body(request: Request, max_bytes: int, domain_error: type[Exception],
                           too_large: Callable[[int], Exception], unreadable: Callable[[], Exception]) -> bytes:
    """Streams the body and stops at max_bytes, without trusting Content-Length."""
    try:
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > max_bytes:
            raise too_large(max_bytes)
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > max_bytes:
                raise too_large(max_bytes)
            chunks.append(chunk)
        return b"".join(chunks)
    except domain_error:
        raise
    except ClientDisconnect as exc:
        logger.info("client went away while sending a body")
        raise unreadable() from exc
    except Exception as exc:
        logger.warning("could not read a request body type=%s", type(exc).__name__)
        raise unreadable() from exc


async def webnest_user_id(request: Request, auth_service, session, email_enabled: bool,
                          domain_error: type[Exception], login_required: Callable[[], Exception],
                          unavailable: Callable[[], Exception]) -> uuid.UUID:
    """The learner from X-WebNest-Authorization, checked by the same
    AuthService.get_current_user that get_current_user uses, with login's own
    verification rule (verified email only while email is switched on).
    The session is handed back straight away, so no database connection is
    held while the slow upstream call runs."""
    try:
        scheme, _, token = request.headers.get("x-webnest-authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise login_required()
        user = await auth_service.get_current_user(token.strip())
        if not user.is_active or (email_enabled and not user.is_verified):
            raise login_required()
        user_id = user.id
        await session.close()
        return user_id
    except domain_error:
        raise
    except (UnauthorizedError, ValueError) as exc:
        raise login_required() from exc
    except Exception as exc:
        logger.error("login check failed type=%s", type(exc).__name__)
        raise unavailable() from exc
