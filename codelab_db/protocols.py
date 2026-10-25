"""The interfaces services depend on, and the plain data passed between them.
Concrete classes live in infra/ and services/ and are built only in
dependencies.py. Passwords are kept out of every repr."""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class DbUser:
    id: uuid.UUID


@dataclass(frozen=True)
class HostRule:
    pattern: str        # "*" matches exactly one DNS label, e.g. "*.pooler.supabase.com"
    tls_verify: bool    # verify the certificate against public CAs (False: encrypted, not verified)
    label: str          # provider name shown to learners
    domain: str         # the fixed part of the pattern, the only host detail ever logged


@dataclass(frozen=True)
class ParsedUrl:
    """A connection URL that passed syntax and allowlist checks. No network yet."""

    engine: str
    srv: bool
    hosts: tuple[tuple[str, int | None], ...]
    user: str | None
    password: str | None = field(repr=False)
    database: str | None
    options: dict[str, str]
    rule: HostRule


@dataclass(frozen=True)
class Target:
    """Where to connect: every address was checked to be public."""

    engine: str
    host: str
    port: int | None
    address: str | None                 # the checked IP to connect to (SQL engines)
    user: str | None
    password: str | None = field(repr=False)
    database: str | None
    tls_verify: bool
    srv: bool
    # Every host was allowlist- and public-address-checked; for mongodb+srv
    # these are the SRV record's hosts, so the driver never looks SRV up again.
    hosts: tuple[tuple[str, int | None], ...]
    options: dict[str, str]
    domain: str


@dataclass(frozen=True)
class Limits:
    statement_timeout_seconds: int
    connect_timeout_seconds: int
    max_rows: int
    max_bytes: int
    ca_file: str | None


@dataclass(frozen=True)
class SqlStatement:
    sql: str = field(repr=False)
    params: Any = field(repr=False)
    kind: str             # "cursor" (server-side cursor, SELECT-like) or "execute"
    statement_type: str   # first keyword, e.g. SELECT, for logs


@dataclass(frozen=True)
class MongoOperation:
    operation: str
    database: str
    collection: str | None
    filter: dict | None = field(repr=False)
    update: Any = field(repr=False)
    document: dict | None = field(repr=False)
    documents: list | None = field(repr=False)
    pipeline: list | None = field(repr=False)
    projection: dict | None = field(repr=False)
    sort: list | None
    limit: int
    skip: int
    upsert: bool


@dataclass(frozen=True)
class SqlResult:
    columns: list[str]
    rows: list[list]
    row_count: int


@dataclass(frozen=True)
class MongoResult:
    documents: list
    count: int
    write: dict | None


@dataclass(frozen=True)
class ServerInfo:
    server_version: str


class HostAllowlist(Protocol):
    def match(self, engine: str, host: str) -> HostRule | None: ...
    def ports(self, engine: str) -> tuple[int, ...]: ...
    def default_port(self, engine: str) -> int: ...
    def describe(self) -> dict: ...


class HostResolver(Protocol):
    # Every address of the host, all public, or raises HostNotAllowed / ConnectFailed.
    async def resolve_public(self, host: str) -> list[str]: ...
    # (target host, port) pairs of an SRV record, or raises ConnectFailed.
    async def resolve_srv(self, name: str) -> list[tuple[str, int]]: ...
    # The TXT strings of a name; [] when there are none (TXT is optional).
    async def resolve_txt(self, name: str) -> list[str]: ...


class Capacity(Protocol):
    # False while every database worker thread is busy (including threads
    # still finishing a request that already timed out).
    def has_room(self) -> bool: ...


class DatabaseConnector(Protocol):
    async def run(self, target: Target, request: Any, limits: Limits) -> SqlResult | MongoResult: ...
    async def test(self, target: Target, limits: Limits) -> ServerInfo: ...


class StatementGuard(Protocol):
    # Returns what the connector needs (SqlStatement / MongoOperation), or raises.
    def check(self, request: Any, database_from_url: str | None) -> Any: ...


class RateLimiter(Protocol):
    # Refuses early without counting (cheap, before any network work).
    def check(self, user_id: uuid.UUID, now: datetime) -> None: ...
    # Counts a query that is about to reach the database.
    def consume(self, user_id: uuid.UUID, now: datetime) -> None: ...


class Clock(Protocol):
    def now(self) -> datetime: ...
