"""HTTP only: parse, call a service, map domain errors to HTTP (section 3.8)."""

import logging

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse

from codelab_relay.constants import RELAY_METHODS, RELAY_PATH
from codelab_relay.dependencies import (
    check_relay_open,
    get_body_reader,
    get_provider_list_service,
    get_relay_service,
    get_relay_user,
    get_response_builder,
    get_usage_service,
)
from codelab_relay.errors import RelayError, UpstreamUnavailable
from codelab_relay.protocols import RelayUser
from codelab_relay.schemas import ProvidersResponse, UsageResponse
from codelab_relay.services.header_filter import SECURITY_HEADERS
from codelab_relay.services.http_io import BodyReader, ResponseBuilder
from codelab_relay.services.provider_list_service import ProviderListService
from codelab_relay.services.relay_service import RelayService
from codelab_relay.services.usage_service import UsageService

logger = logging.getLogger("webnest.codelab_relay")

router = APIRouter(prefix=RELAY_PATH, tags=["codelab-relay"])

# Routed here only so they get the contract's 405 (from check_relay_open,
# before login or body reading) instead of Starlette's bare one. CORS
# preflights never reach a route; the CORS middleware answers them.
_REFUSED_METHODS = ("HEAD", "OPTIONS", "TRACE")


def relay_error_response(error: RelayError) -> JSONResponse:
    try:
        headers = {**error.headers, **SECURITY_HEADERS, "X-WebNest-Relay-Error": error.code}
        return JSONResponse(status_code=error.http_status,
                            content={"detail": error.message, "code": error.code, **error.extra}, headers=headers)
    except Exception:
        logger.exception("could not render relay error code=%s", getattr(error, "code", "?"))
        return JSONResponse(status_code=502, content={"detail": "The relay could not complete this call.",
                                                      "code": "relay_upstream_unavailable", "provider": "unknown"},
                            headers={**SECURITY_HEADERS, "X-WebNest-Relay-Error": "relay_upstream_unavailable"})


async def _relay_error_handler(request: Request, exc: Exception) -> JSONResponse:
    try:
        if isinstance(exc, RelayError):
            return relay_error_response(exc)
        return relay_error_response(UpstreamUnavailable("unknown"))
    except Exception:
        logger.exception("relay error handler failed")
        raise


def register_relay_error_handlers(app: FastAPI) -> None:
    try:
        app.add_exception_handler(RelayError, _relay_error_handler)
    except Exception:
        logger.critical("could not register the relay error handler", exc_info=True)
        raise


@router.get("/providers", response_model=ProvidersResponse)
async def list_providers(response: Response, service: ProviderListService = Depends(get_provider_list_service)):
    try:
        response.headers["Cache-Control"] = "public, max-age=300"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return service.describe()
    except RelayError:
        raise
    except Exception as exc:
        logger.exception("unexpected error in list_providers")
        raise UpstreamUnavailable("unknown") from exc


@router.get("/usage", response_model=UsageResponse)
async def usage(response: Response, user: RelayUser = Depends(get_relay_user),
                service: UsageService = Depends(get_usage_service)):
    try:
        response.headers.update(SECURITY_HEADERS)
        return await service.for_user(user.id)
    except RelayError:
        raise
    except Exception as exc:
        logger.exception("unexpected error in usage")
        raise UpstreamUnavailable("unknown") from exc


# Declared after /providers and /usage so the fixed routes win. check_relay_open
# is the first dependency, so FastAPI runs it before the login check.
@router.api_route("/{host}/{path:path}", methods=[*RELAY_METHODS, *_REFUSED_METHODS], include_in_schema=False)
async def relay(host: str, path: str, request: Request, _open: None = Depends(check_relay_open),
                user: RelayUser = Depends(get_relay_user),
                service: RelayService = Depends(get_relay_service),
                reader: BodyReader = Depends(get_body_reader),
                builder: ResponseBuilder = Depends(get_response_builder)) -> Response:
    try:
        body = await reader.read(request)           # streams with the size cap, raises PayloadTooLarge
        upstream, provider, remaining = await service.relay(
            user.id, request.method, host.lower(), path, request.url.query,
            {name.lower(): value for name, value in request.headers.items()}, body)
        status, headers, content = builder.build(upstream, provider, remaining)
        return Response(content=content, status_code=status, headers=headers)
    except RelayError:
        raise
    except Exception as exc:
        logger.exception("unexpected error in relay host=%r", host[:100])
        raise UpstreamUnavailable("unknown") from exc
