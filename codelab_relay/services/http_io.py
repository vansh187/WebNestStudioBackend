"""Reading the learner's request body and shaping the response sent back to
the browser (sections 3.6 and 3.8)."""

import logging

from fastapi import Request
from starlette.requests import ClientDisconnect

from codelab_relay.errors import BadTarget, PayloadTooLarge, RelayError, UpstreamUnavailable
from codelab_relay.protocols import Provider, UpstreamResponse
from codelab_relay.services.header_filter import SECURITY_HEADERS, HeaderFilter

logger = logging.getLogger("webnest.codelab_relay")


class BodyReader:
    """Streams the body with the size cap instead of trusting Content-Length
    (security fix 9)."""

    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max_bytes

    async def read(self, request: Request) -> bytes:
        try:
            declared = request.headers.get("content-length")
            if declared is not None and declared.isdigit() and int(declared) > self._max_bytes:
                raise PayloadTooLarge(self._max_bytes)
            chunks: list[bytes] = []
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > self._max_bytes:
                    raise PayloadTooLarge(self._max_bytes)
                chunks.append(chunk)
            return b"".join(chunks)
        except RelayError:
            raise
        except ClientDisconnect as exc:
            logger.info("client went away while sending a relay body")
            raise BadTarget() from exc
        except Exception as exc:
            logger.exception("could not read a relay request body")
            raise BadTarget() from exc


class ResponseBuilder:
    def __init__(self, header_filter: HeaderFilter) -> None:
        self._filter = header_filter

    def build(self, upstream: UpstreamResponse, provider: Provider, remaining: int) -> tuple[int, dict[str, str], bytes]:
        """The provider's status and body unchanged, behind the response header
        allowlist, plus the relay's own headers. Never X-WebNest-Relay-Error."""
        try:
            headers = self._filter.incoming(provider, upstream.headers)
            headers.update(SECURITY_HEADERS)
            headers["X-WebNest-Relay-Provider"] = provider.id
            headers["X-WebNest-Relay-Remaining"] = str(remaining)
            return upstream.status, headers, upstream.body
        except RelayError:
            raise
        except Exception as exc:
            logger.exception("could not build the relay response provider=%s", provider.id)
            raise UpstreamUnavailable(provider.id, provider.name) from exc
