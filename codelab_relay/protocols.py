"""The interfaces services depend on (section 3.2). Concrete classes live in
infra/ and services/ and are constructed only in dependencies.py."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    enabled: bool
    hosts: tuple[str, ...]
    path_prefixes: tuple[str, ...]
    test_mode: dict            # {"rule": "key_prefix" | "sandbox_host", ...} (section 4)
    forward_request_headers: tuple[str, ...]
    forward_response_headers: tuple[str, ...]
    test_key_hint: str
    docs_url: str


@dataclass(frozen=True)
class RelayUser:
    """The logged-in learner. Plain data, so nothing after the login check
    depends on an ORM object or a database session."""

    id: uuid.UUID


@dataclass(frozen=True)
class OutgoingRequest:
    method: str
    url: str                   # always https://<host>/<path>?<query>
    headers: dict[str, str]
    body: bytes
    # The public IP the resolver checked. The connection goes to exactly this
    # address (TLS still verifies the host name), so a second DNS answer at
    # connect time can't point the call somewhere else (DNS rebinding).
    connect_address: str | None = None


@dataclass(frozen=True)
class UpstreamResponse:
    status: int
    headers: dict[str, str]    # lowercase names
    body: bytes


class ProviderRegistry(Protocol):
    def all_enabled(self) -> list[Provider]: ...
    def for_host(self, host: str) -> Provider | None: ...   # includes disabled ones


class UpstreamClient(Protocol):
    async def send(self, request: OutgoingRequest, max_response_bytes: int, timeout_seconds: float) -> UpstreamResponse: ...


class RateLimiter(Protocol):
    async def consume(self, user_id: uuid.UUID, now: datetime) -> int: ...   # calls left this minute, or raises RateLimited
    async def refund(self, user_id: uuid.UUID, now: datetime) -> None: ...   # gives back the day's call (not the minute's)
    async def remaining(self, user_id: uuid.UUID, now: datetime) -> dict: ...


class MinuteLimiter(Protocol):
    async def take(self, user_id: uuid.UUID, now: datetime) -> int: ...   # calls left this minute, or raises RateLimited
    async def give_back(self, user_id: uuid.UUID, now: datetime) -> None: ...
    async def remaining(self, user_id: uuid.UUID, now: datetime) -> int: ...


class DailyCounter(Protocol):
    async def take(self, user_id: uuid.UUID, now: datetime) -> int: ...   # calls left today, or raises RateLimited
    async def give_back(self, user_id: uuid.UUID, now: datetime) -> None: ...
    async def remaining(self, user_id: uuid.UUID, now: datetime) -> int: ...


class HostResolver(Protocol):
    # Every address the host resolves to, all public, or raises
    # UpstreamUnavailable for private/loopback/reserved IPs.
    async def resolve_public(self, host: str) -> list[str]: ...


class TestModeRule(Protocol):
    def check(self, provider: Provider, headers: dict[str, str], query: str, body: bytes) -> None: ...


class Clock(Protocol):
    def now(self) -> datetime: ...
