"""The only file importing httpx (section 3.7, security fixes 1, 3, 9 and 13).
Never log request headers, bodies or the URL's query string here."""

import asyncio
import ipaddress
import logging
from collections.abc import Callable
from urllib.parse import urlsplit, urlunsplit

import httpx

from codelab_relay.errors import RelayError, ResponseTooLarge, UpstreamTimeout, UpstreamUnavailable
from codelab_relay.protocols import OutgoingRequest, UpstreamResponse

logger = logging.getLogger("webnest.codelab_relay")

MAX_CONNECTIONS = 50


def _host_of(url: str) -> str:
    try:
        return urlsplit(url).hostname or "unknown"
    except ValueError:
        return "unknown"


def create_shared_httpx_client(connect_timeout_seconds: float, timeout_seconds: float) -> httpx.AsyncClient:
    try:
        return httpx.AsyncClient(
            follow_redirects=False,
            verify=True,
            trust_env=False,   # environment proxy variables can never reroute calls
            timeout=httpx.Timeout(timeout_seconds, connect=connect_timeout_seconds),
            limits=httpx.Limits(max_connections=MAX_CONNECTIONS, max_keepalive_connections=10),
        )
    except Exception as exc:
        logger.exception("could not create the relay HTTP client")
        raise UpstreamUnavailable("unknown") from exc


def pin_to_address(request: OutgoingRequest) -> tuple[str, dict[str, str], dict]:
    """The URL, headers and httpx extensions that send the request to
    request.connect_address while TLS (SNI and certificate check) and the
    Host header still use the real host name."""
    try:
        if not request.connect_address:
            return request.url, request.headers, {}
        parts = urlsplit(request.url)
        host = parts.hostname or ""
        address = ipaddress.ip_address(request.connect_address)
        netloc = f"[{address}]" if address.version == 6 else str(address)
        url = urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))
        return url, {**request.headers, "host": host}, {"sni_hostname": host}
    except ValueError as exc:
        logger.error("relay connect address is not an IP host=%s", _host_of(request.url))
        raise UpstreamUnavailable(_host_of(request.url)) from exc


class HttpxUpstreamClient:
    """Owns its httpx.AsyncClient: built on first use, rebuilt after aclose()
    or when used from another event loop (an httpx pool is tied to the loop
    that opened its connections), so a restart never leaves it closed."""

    def __init__(self, make_client: Callable[[], httpx.AsyncClient]) -> None:
        self._make_client = make_client   # follow_redirects=False, verify=True, trust_env=False
        self._client: httpx.AsyncClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _current_client(self) -> httpx.AsyncClient:
        try:
            loop = asyncio.get_running_loop()
            if self._client is None or self._client.is_closed or self._loop is not loop:
                if self._client is not None and not self._client.is_closed:
                    logger.info("relay HTTP client rebuilt for a new event loop")
                self._client = self._make_client()
                self._loop = loop
            return self._client
        except RelayError:
            raise
        except Exception as exc:
            logger.exception("could not prepare the relay HTTP client")
            raise UpstreamUnavailable("unknown") from exc

    async def send(self, request: OutgoingRequest, max_response_bytes: int, timeout_seconds: float) -> UpstreamResponse:
        try:
            url, request_headers, extensions = pin_to_address(request)
            async with asyncio.timeout(timeout_seconds):
                async with self._current_client().stream(request.method, url, headers=request_headers,
                                                         content=request.body or None,
                                                         extensions=extensions) as response:
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > max_response_bytes:
                            raise ResponseTooLarge(max_response_bytes)
                        chunks.append(chunk)
                    headers = {name.lower(): value for name, value in response.headers.items()}
                    return UpstreamResponse(response.status_code, headers, b"".join(chunks))
        except RelayError:
            raise
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise UpstreamTimeout(_host_of(request.url), timeout_seconds) from exc
        except httpx.HTTPError as exc:
            logger.warning("upstream error host=%s type=%s", _host_of(request.url), type(exc).__name__)
            raise UpstreamUnavailable(_host_of(request.url)) from exc
        except Exception as exc:
            logger.error("unexpected upstream error host=%s type=%s", _host_of(request.url), type(exc).__name__)
            raise UpstreamUnavailable(_host_of(request.url)) from exc

    async def aclose(self) -> None:
        try:
            client, self._client, self._loop = self._client, None, None
            if client is not None and not client.is_closed:
                await client.aclose()
        except Exception:
            logger.warning("could not close the relay HTTP client", exc_info=True)
