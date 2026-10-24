"""Host/path validation and the upstream URL (section 3.3, security fixes 1 and 10)."""

import ipaddress
import logging
import re
from urllib.parse import urlsplit

from codelab_relay.constants import HOST_PATTERN
from codelab_relay.errors import BadTarget, HostNotAllowed, PathNotAllowed, ProviderDisabled, RelayError
from codelab_relay.protocols import Provider, ProviderRegistry

logger = logging.getLogger("webnest.codelab_relay")

# The path arrives percent-decoded, so "?" and "#" here came from %3F / %23:
# they would smuggle a query (past the live-key scan of the real query) or a
# fragment into the upstream URL. Their encoded forms catch double encoding.
_BAD_PATH = re.compile(r"(^|/)\.\.?(/|$)|//|\\|\?|#|%2e|%2f|%5c|%00|%3f|%23", re.IGNORECASE)
MAX_PATH_LENGTH = 2048


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _has_unsafe_chars(text: str) -> bool:
    try:
        return any(ord(char) < 0x21 or ord(char) > 0x7E for char in text)
    except TypeError as exc:
        raise BadTarget() from exc


class TargetParser:
    def __init__(self, registry: ProviderRegistry) -> None:
        self._registry = registry

    def parse(self, host: str, path: str, query: str) -> tuple[Provider, str]:
        try:
            if len(host) > 253 or not HOST_PATTERN.fullmatch(host) or _is_ip_literal(host):
                raise BadTarget()
            if len(path) > MAX_PATH_LENGTH or _BAD_PATH.search(path) or _has_unsafe_chars(path):
                raise BadTarget()
            if _has_unsafe_chars(query):
                raise BadTarget()
            provider = self._registry.for_host(host)
            if provider is None:
                raise HostNotAllowed(host)
            if not provider.enabled:
                raise ProviderDisabled(provider.id)
            clean_path = "/" + path
            if not any(clean_path.startswith(prefix) for prefix in provider.path_prefixes):
                raise PathNotAllowed(provider.id)
            url = f"https://{host}{clean_path}" + (f"?{query}" if query else "")
            # Belt and braces: the URL must still point at the same host after parsing.
            parts = urlsplit(url)
            if (parts.hostname != host or parts.port is not None or parts.username or parts.password
                    or parts.path != clean_path or parts.query != query or parts.fragment):
                raise BadTarget()
            return provider, url
        except RelayError:
            raise
        except Exception as exc:
            logger.exception("target parse failed host=%r", host[:100])
            raise BadTarget() from exc
