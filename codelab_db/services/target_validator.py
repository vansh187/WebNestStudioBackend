"""Connection URL → Target (B5.3, "No SSRF").

parse(): syntax, engine/scheme match, host allowlist, ports, allowed URL
options. No network. The URL is parsed here by hand and never handed to a
driver, so options such as libpq's host=/hostaddr=/sslmode=disable or
pymongo's proxyHost can't redirect or weaken the connection.

resolve(): DNS. Every address (and every host of a MongoDB SRV record) must
be public; SQL connections are then pinned to the checked address."""

import asyncio
import ipaddress
import logging
import re
from urllib.parse import parse_qsl, unquote

from codelab_db.constants import HOST_PATTERN, LABEL_PATTERN, LOGGER_NAME
from codelab_db.errors import BadUrl, ConnectFailed, DbError, HostNotAllowed
from codelab_db.protocols import HostAllowlist, HostResolver, ParsedUrl, Target

logger = logging.getLogger(LOGGER_NAME)

SCHEMES = {
    "postgres": ("postgresql", "postgres"),
    "mysql": ("mysql",),
    "mongodb": ("mongodb", "mongodb+srv"),
}
MAX_CREDENTIAL_LENGTH = 256
_NEON_ENDPOINT = re.compile(r"^endpoint=[a-z0-9-]{1,64}$")
_TLS_REQUIRED = "TLS is required: remove the option that turns it off"


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def _postgres_options(raw: dict[str, str]) -> dict[str, str]:
    try:
        options = {}
        for key, value in raw.items():
            if key == "sslmode":
                if value not in ("require", "verify-ca", "verify-full", "prefer"):
                    raise BadUrl(_TLS_REQUIRED)
            elif key == "channel_binding":
                if value not in ("require", "prefer", "disable"):
                    raise BadUrl("channel_binding must be require, prefer or disable")
                options["channel_binding"] = value
            elif key == "options":
                if not _NEON_ENDPOINT.match(value):
                    raise BadUrl("only options=endpoint=<id> (Neon) is supported")
                options["options"] = value
            elif key in ("connect_timeout", "application_name"):
                continue   # the proxy sets these itself
            else:
                raise BadUrl(f"the URL option {key!r} isn't supported")
        return options
    except DbError:
        raise
    except Exception as exc:
        raise BadUrl("the URL options could not be read") from exc


def _mysql_options(raw: dict[str, str]) -> dict[str, str]:
    try:
        for key, value in raw.items():
            if key.lower() in ("ssl-mode", "sslmode", "ssl_mode"):
                if value.upper() not in ("REQUIRED", "VERIFY_CA", "VERIFY_IDENTITY", "PREFERRED"):
                    raise BadUrl(_TLS_REQUIRED)
            elif key in ("ssl", "sslaccept", "charset"):
                if key == "ssl" and value.strip().lower() in ("false", "0", "off"):
                    raise BadUrl(_TLS_REQUIRED)
                continue   # TLS is always on; the proxy chooses the charset
            else:
                raise BadUrl(f"the URL option {key!r} isn't supported")
        return {}
    except DbError:
        raise
    except Exception as exc:
        raise BadUrl("the URL options could not be read") from exc


_MONGO_PASSTHROUGH = {"retrywrites": "retryWrites", "w": "w", "authsource": "authSource",
                      "authmechanism": "authMechanism", "replicaset": "replicaSet", "retryreads": "retryReads",
                      "readpreference": "readPreference"}


def _mongo_options(raw: dict[str, str]) -> dict[str, str]:
    try:
        options = {}
        for key, value in raw.items():
            lowered = key.lower()
            if lowered in ("tls", "ssl"):
                if value.lower() != "true":
                    raise BadUrl(_TLS_REQUIRED)
            elif lowered == "appname":
                continue   # the proxy names itself
            elif lowered == "authmechanism" and value not in ("SCRAM-SHA-1", "SCRAM-SHA-256"):
                raise BadUrl("only SCRAM-SHA-1 and SCRAM-SHA-256 authentication are supported")
            elif lowered in _MONGO_PASSTHROUGH:
                if len(value) > 128:
                    raise BadUrl(f"the URL option {key!r} is too long")
                options[_MONGO_PASSTHROUGH[lowered]] = value
            else:
                raise BadUrl(f"the URL option {key!r} isn't supported")
        return options
    except DbError:
        raise
    except Exception as exc:
        raise BadUrl("the URL options could not be read") from exc


OPTION_PARSERS = {"postgres": _postgres_options, "mysql": _mysql_options, "mongodb": _mongo_options}


def _leaves(group: BaseExceptionGroup) -> list[BaseException]:
    try:
        found: list[BaseException] = []
        stack = list(group.exceptions)
        while stack:
            error = stack.pop(0)
            if isinstance(error, BaseExceptionGroup):
                stack.extend(error.exceptions)
            else:
                found.append(error)
        return found
    except Exception:
        return []


async def gather_all(coroutines: list) -> list:
    """Runs DNS lookups at the same time. The first failure cancels the rest
    (TaskGroup) and is raised as-is, so callers see one domain error."""
    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(coroutine) for coroutine in coroutines]
        return [task.result() for task in tasks]
    except BaseExceptionGroup as failures:
        leaves = _leaves(failures)
        first = next((error for error in leaves if isinstance(error, DbError)), None)
        if first is not None:
            raise first from None
        if any(isinstance(error, asyncio.CancelledError) for error in leaves):
            raise asyncio.CancelledError() from None
        raise ConnectFailed("the database host could not be looked up") from failures


class TargetValidator:
    def __init__(self, allowlist: HostAllowlist, resolver: HostResolver, dns_timeout_seconds: float = 5.0) -> None:
        self._allowlist = allowlist
        self._resolver = resolver
        self._dns_timeout_seconds = dns_timeout_seconds

    def parse(self, engine: str, url: str) -> ParsedUrl:
        try:
            if engine not in SCHEMES:
                raise BadUrl(f"unknown engine {engine!r}")
            if any(ord(char) < 0x21 or ord(char) == 0x7F for char in url):
                raise BadUrl("it contains spaces or control characters (percent-encode them)")
            scheme, sep, rest = url.partition("://")
            scheme = scheme.lower()
            if not sep or scheme not in SCHEMES[engine]:
                expected = " or ".join(f"{name}://" for name in SCHEMES[engine])
                raise BadUrl(f"a {engine} URL must start with {expected}")
            if "#" in rest:
                raise BadUrl("it contains '#' (percent-encode it as %23)")
            end = min((index for index in (rest.find("/"), rest.find("?")) if index >= 0), default=len(rest))
            netloc, tail = rest[:end], rest[end:]
            if "@" in tail.partition("?")[0]:
                raise BadUrl("percent-encode special characters in the user name or password (/ ? # @ :)")
            userinfo, at, hostinfo = netloc.rpartition("@")
            user, password = None, None
            if at:
                raw_user, colon, raw_password = userinfo.partition(":")
                user = unquote(raw_user, errors="strict")
                password = unquote(raw_password, errors="strict") if colon else None
                for value in (user, password):
                    if value is not None and (len(value) > MAX_CREDENTIAL_LENGTH or "\x00" in value):
                        raise BadUrl("the user name or password is too long or contains a NUL character")
            if engine != "mongodb" and not user:
                raise BadUrl("the user name is missing (user:password@host)")
            srv = scheme == "mongodb+srv"
            hosts = self._hosts(engine, hostinfo, srv)
            path, _, query = tail.partition("?")
            database = unquote(path[1:], errors="strict") if path.startswith("/") and len(path) > 1 else None
            if database is not None and ("/" in database or "\x00" in database or len(database) > 128):
                raise BadUrl("the database name in the URL path is not valid")
            pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=False) if query else []
            raw_options: dict[str, str] = {}
            for key, value in pairs:
                if key in raw_options:
                    raise BadUrl(f"the URL option {key!r} appears twice")
                raw_options[key] = value
            options = OPTION_PARSERS[engine](raw_options)
            rule = self._allowlist.match(engine, hosts[0][0])
            return ParsedUrl(engine, srv, hosts, user, password, database, options, rule)
        except DbError:
            raise
        except UnicodeDecodeError as exc:
            raise BadUrl("it contains a percent-encoded byte sequence that isn't UTF-8") from exc
        except Exception as exc:
            logger.warning("connection URL could not be parsed engine=%s type=%s", engine, type(exc).__name__)
            raise BadUrl("it could not be read") from exc

    def _hosts(self, engine: str, hostinfo: str, srv: bool) -> tuple[tuple[str, int | None], ...]:
        try:
            if not hostinfo:
                raise BadUrl("the host is missing")
            parts = hostinfo.split(",")
            if len(parts) > 1 and engine != "mongodb":
                raise BadUrl("only one host is allowed")
            if srv and len(parts) > 1:
                raise BadUrl("a mongodb+srv:// URL has exactly one host")
            if len(parts) > 10:
                raise BadUrl("at most 10 hosts are allowed")
            hosts = []
            for part in parts:
                if part.startswith("["):
                    raise HostNotAllowed(part, "is an IP address; use the provider's host name")
                host, colon, port_text = part.partition(":")
                host = host.lower()
                if not host:
                    raise BadUrl("a host is empty")
                if _is_ip_literal(host):
                    raise HostNotAllowed(host, "is an IP address; use the provider's host name")
                if LABEL_PATTERN.fullmatch(host):   # localhost, a container name, ...
                    raise HostNotAllowed(host, "is a local host name; use your cloud database's host")
                if len(host) > 253 or not HOST_PATTERN.fullmatch(host):
                    raise BadUrl(f"{host[:100]!r} is not a valid host name")
                if self._allowlist.match(engine, host) is None:
                    raise HostNotAllowed(host)
                port = None
                if colon:
                    if srv:
                        raise BadUrl("a mongodb+srv:// URL can't have a port")
                    if not port_text.isdigit() or len(port_text) > 5:
                        raise BadUrl("the port must be a number")
                    port = int(port_text)
                elif not srv:
                    port = self._allowlist.default_port(engine)
                if port is not None and port not in self._allowlist.ports(engine):
                    allowed = ", ".join(str(value) for value in self._allowlist.ports(engine))
                    raise HostNotAllowed(f"{host}:{port}", f"uses a port the proxy doesn't allow (allowed: {allowed})")
                hosts.append((host, port))
            return tuple(hosts)
        except DbError:
            raise
        except Exception as exc:
            raise BadUrl("the host could not be read") from exc

    async def resolve(self, parsed: ParsedUrl) -> Target:
        """All lookups run in parallel within one DNS budget; running out of
        it is a connection problem, not a slow query."""
        try:
            async with asyncio.timeout(self._dns_timeout_seconds):
                hosts, options = parsed.hosts, dict(parsed.options)
                if parsed.srv:
                    hosts, txt_options = await self._srv_hosts(parsed.hosts[0][0])
                    options = {**txt_options, **options}   # URL options win over TXT ones
                addresses = await gather_all([self._resolver.resolve_public(host) for host, _ in hosts])
            first_host, first_port = hosts[0]
            return Target(
                engine=parsed.engine, host=first_host, port=first_port,
                address=addresses[0][0] if parsed.engine != "mongodb" else None,
                user=parsed.user, password=parsed.password, database=parsed.database,
                tls_verify=parsed.rule.tls_verify, srv=parsed.srv, hosts=tuple(hosts), options=options,
                domain=parsed.rule.domain,
            )
        except DbError:
            raise
        except TimeoutError as exc:
            raise ConnectFailed("looking up the database host timed out") from exc
        except Exception as exc:
            logger.warning("target resolution failed engine=%s type=%s", parsed.engine, type(exc).__name__)
            raise HostNotAllowed(parsed.rule.domain, "could not be checked") from exc

    async def _srv_hosts(self, host: str) -> tuple[tuple[tuple[str, int], ...], dict[str, str]]:
        """Every host in the SRV record must itself be an allowed host and
        port. The TXT record (looked up at the same time) may only carry
        authSource, replicaSet and loadBalanced, as the MongoDB spec allows."""
        try:
            records, texts = await gather_all([self._resolver.resolve_srv(f"_mongodb._tcp.{host}"),
                                               self._resolver.resolve_txt(host)])
            if not records:
                raise HostNotAllowed(host, "has no MongoDB SRV record")
            checked = []
            for target, port in records[:10]:
                target = target.rstrip(".").lower()
                if not HOST_PATTERN.fullmatch(target) or self._allowlist.match("mongodb", target) is None:
                    raise HostNotAllowed(host, "lists a database host the proxy doesn't allow")
                if port not in self._allowlist.ports("mongodb"):
                    raise HostNotAllowed(host, "lists a port the proxy doesn't allow")
                checked.append((target, port))
            return tuple(checked), _txt_options(texts)
        except DbError:
            raise
        except Exception as exc:
            raise HostNotAllowed(host, "could not be checked") from exc


_TXT_KEYS = {"authsource": "authSource", "replicaset": "replicaSet", "loadbalanced": "loadBalanced"}


def _txt_options(texts: list[str]) -> dict[str, str]:
    """Options from a MongoDB SRV cluster's TXT record. Anything else in it is
    ignored, as drivers must."""
    try:
        options: dict[str, str] = {}
        for text in texts[:4]:
            for key, value in parse_qsl(text[:1024], keep_blank_values=False):
                name = _TXT_KEYS.get(key.lower())
                if name and len(value) <= 128 and all(ord(char) > 0x20 for char in value):
                    options[name] = value
        return options
    except Exception:
        logger.info("MongoDB TXT options could not be read; ignoring them")
        return {}
