"""The only file doing DNS (socket for A/AAAA, dnspython for SRV). Refuses a
host whose answers include a non-public address, so an allowlisted name can
never be pointed at internal services or cloud metadata."""

import asyncio
import ipaddress
import logging
import socket
from collections.abc import Awaitable, Callable

import dns.asyncresolver
import dns.exception
import dns.resolver

from codelab_db.constants import LOGGER_NAME
from codelab_db.errors import ConnectFailed, DbError, HostNotAllowed

logger = logging.getLogger(LOGGER_NAME)

AddressLookup = Callable[[str], Awaitable[list[str]]]
SrvLookup = Callable[[str], Awaitable[list[tuple[str, int]]]]
TxtLookup = Callable[[str], Awaitable[list[str]]]
DNS_TIMEOUT_SECONDS = 5.0


def is_public_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])   # drop an IPv6 zone id
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        return ip.is_global and not ip.is_multicast
    except ValueError:
        return False


def _ipv4_first(addresses: list[str]) -> list[str]:
    """Render may have no IPv6 route, so IPv4 answers are tried first."""
    try:
        return sorted(addresses, key=lambda address: ":" in address)
    except TypeError:
        return addresses


class DnsHostResolver:
    def __init__(self, lookup: AddressLookup | None = None, srv_lookup: SrvLookup | None = None,
                 txt_lookup: TxtLookup | None = None) -> None:
        self._lookup = lookup or self._system_lookup
        self._srv_lookup = srv_lookup or self._system_srv_lookup
        self._txt_lookup = txt_lookup or self._system_txt_lookup

    async def resolve_public(self, host: str) -> list[str]:
        try:
            addresses = list(dict.fromkeys(await self._lookup(host)))   # unique, in answer order
            if not addresses:
                raise ConnectFailed(f"{host} has no address")
            if not all(isinstance(address, str) and is_public_address(address) for address in addresses):
                logger.warning("database host resolved to a non-public address host=%s", host)
                raise HostNotAllowed(host, "resolves to a private or reserved address")
            return _ipv4_first(addresses)
        except DbError:
            raise
        except (OSError, UnicodeError) as exc:
            logger.info("database DNS lookup failed host=%s type=%s", host, type(exc).__name__)
            raise ConnectFailed(f"the host {host} could not be found") from exc
        except Exception as exc:
            logger.warning("database DNS check failed host=%s type=%s", host, type(exc).__name__)
            raise ConnectFailed(f"the host {host} could not be checked") from exc

    async def resolve_srv(self, name: str) -> list[tuple[str, int]]:
        try:
            return list(await self._srv_lookup(name))
        except DbError:
            raise
        except Exception as exc:
            logger.info("database SRV lookup failed type=%s", type(exc).__name__)
            raise ConnectFailed("the MongoDB SRV record could not be read") from exc

    async def resolve_txt(self, name: str) -> list[str]:
        """TXT is optional for a MongoDB cluster: any failure means "none"."""
        try:
            return [text for text in await self._txt_lookup(name) if isinstance(text, str)]
        except Exception as exc:
            logger.info("database TXT lookup gave nothing type=%s", type(exc).__name__)
            return []

    async def _system_txt_lookup(self, name: str) -> list[str]:
        try:
            answer = await dns.asyncresolver.resolve(name, "TXT", lifetime=DNS_TIMEOUT_SECONDS)
            return [b"".join(record.strings).decode("ascii", "replace") for record in answer]
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers, dns.exception.Timeout):
            return []

    async def _system_lookup(self, host: str) -> list[str]:
        try:
            async with asyncio.timeout(DNS_TIMEOUT_SECONDS):
                infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
            return [info[4][0] for info in infos]
        except socket.gaierror as exc:
            raise ConnectFailed(f"the host {host} could not be found") from exc
        except TimeoutError as exc:
            raise ConnectFailed(f"looking up {host} timed out") from exc

    async def _system_srv_lookup(self, name: str) -> list[tuple[str, int]]:
        try:
            answer = await dns.asyncresolver.resolve(name, "SRV", lifetime=DNS_TIMEOUT_SECONDS)
            return [(str(record.target), int(record.port)) for record in answer]
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers) as exc:
            raise ConnectFailed("the cluster host has no MongoDB SRV record (check the URL)") from exc
        except dns.exception.Timeout as exc:
            raise ConnectFailed("looking up the MongoDB SRV record timed out") from exc
