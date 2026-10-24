"""The only file importing socket. Refuses a host whose DNS answers include a
non-public address (security fix 1), so a registry host can never be pointed
at internal services or cloud metadata. The checked addresses are returned so
the upstream call connects to one of them and never resolves the host again."""

import asyncio
import ipaddress
import logging
import socket
from collections.abc import Awaitable, Callable

from codelab_relay.errors import RelayError, UpstreamUnavailable

logger = logging.getLogger("webnest.codelab_relay")

Lookup = Callable[[str], Awaitable[list[str]]]


def is_public_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])   # drop an IPv6 zone id
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        return ip.is_global and not ip.is_multicast
    except ValueError:
        return False


class DnsHostResolver:
    def __init__(self, lookup: Lookup | None = None) -> None:
        self._lookup = lookup or self._system_lookup

    async def resolve_public(self, host: str) -> list[str]:
        try:
            addresses = list(dict.fromkeys(await self._lookup(host)))   # unique, in answer order
            if not addresses:
                raise UpstreamUnavailable(host)
            if not all(isinstance(address, str) and is_public_address(address) for address in addresses):
                logger.warning("relay host resolved to a non-public address host=%s", host)
                raise UpstreamUnavailable(host)
            return addresses
        except RelayError:
            raise
        except (OSError, UnicodeError) as exc:
            logger.warning("relay DNS lookup failed host=%s type=%s", host, type(exc).__name__)
            raise UpstreamUnavailable(host) from exc
        except Exception as exc:
            logger.exception("relay DNS check failed host=%s", host)
            raise UpstreamUnavailable(host) from exc

    async def _system_lookup(self, host: str) -> list[str]:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            return [info[4][0] for info in infos]
        except socket.gaierror as exc:
            raise UpstreamUnavailable(host) from exc
