"""Header allowlists in both directions (section 3.6, security fixes 6, 7 and 8)."""

import logging

from codelab_relay.errors import BadTarget, RelayError, UpstreamUnavailable
from codelab_relay.protocols import Provider

logger = logging.getLogger("webnest.codelab_relay")

USER_AGENT = "WebNest-CodeLab-Relay/1.0"

DROPPED_REQUEST_HEADERS = frozenset({
    "x-webnest-authorization", "cookie", "host", "origin", "referer", "forwarded", "x-real-ip", "via",
    "connection", "keep-alive", "te", "trailer", "transfer-encoding", "upgrade", "content-length", "user-agent",
})
DROPPED_REQUEST_PREFIXES = ("x-forwarded-", "proxy-")

DROPPED_RESPONSE_HEADERS = frozenset({
    "set-cookie", "location", "content-encoding", "content-length", "transfer-encoding",
    "strict-transport-security", "alt-svc",
})

# On every relay response, success or error (section 2.0).
SECURITY_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


class HeaderFilter:
    def outgoing(self, provider: Provider, incoming: dict[str, str]) -> dict[str, str]:
        """Only the provider's allowlisted request headers, never our own token."""
        try:
            allowed = set(provider.forward_request_headers)
            headers: dict[str, str] = {}
            for name, value in incoming.items():
                name = name.lower()
                if name not in allowed or name in DROPPED_REQUEST_HEADERS or name.startswith(DROPPED_REQUEST_PREFIXES):
                    continue
                if "\r" in value or "\n" in value or "\x00" in value:
                    raise BadTarget()
                headers[name] = value
            headers["user-agent"] = USER_AGENT
            return headers
        except RelayError:
            raise
        except Exception as exc:
            logger.exception("outgoing header filter failed provider=%s", provider.id)
            raise BadTarget() from exc

    def incoming(self, provider: Provider, upstream_headers: dict[str, str]) -> dict[str, str]:
        """Only content-type and the provider's allowlisted response headers."""
        try:
            allowed = {"content-type", *provider.forward_response_headers}
            return {
                name: value
                for name, value in upstream_headers.items()
                if name.lower() in allowed and name.lower() not in DROPPED_RESPONSE_HEADERS
            }
        except Exception as exc:
            logger.exception("incoming header filter failed provider=%s", provider.id)
            raise UpstreamUnavailable(provider.id, provider.name) from exc
