"""CodeLab API relay (CODELAB_API_RELAY_BACKEND.md section 8): unit tests with
fakes, router tests through the real app with dependency_overrides, the httpx
client on httpx.MockTransport, the DNS check with a fake lookup, the shipped
registry, and the feature's code rules.

No network and no database."""

import ast
import asyncio
import base64
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("SUPABASE_URL", "postgresql://user:pass@localhost:5432/test")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

import httpx
import pytest
from fastapi.testclient import TestClient

from app import app
from codelab_relay import errors
from codelab_relay.dependencies import (
    get_body_reader,
    get_provider_list_service,
    get_relay_service,
    get_relay_user,
    get_usage_service,
)
from codelab_relay.infra.composite_limiter import CompositeLimiter
from codelab_relay.infra.dns_host_resolver import DnsHostResolver
from codelab_relay.infra.httpx_upstream_client import HttpxUpstreamClient
from codelab_relay.infra.json_provider_registry import (
    KNOWN_LIVE_HOSTS,
    JsonProviderRegistry,
    load_provider_registry,
    parse_providers,
)
from codelab_relay.infra.memory_minute_limiter import MemoryMinuteLimiter
from codelab_relay.infra.system_clock import seconds_until_midnight
from codelab_relay.logging_filter import RelaySecretsFilter, mask_secrets
from codelab_relay.protocols import OutgoingRequest, UpstreamResponse
from codelab_relay.services.header_filter import HeaderFilter
from codelab_relay.services.http_io import BodyReader
from codelab_relay.services.provider_list_service import ProviderListService
from codelab_relay.services.relay_service import RelayService
from codelab_relay.services.target_parser import TargetParser
from codelab_relay.services import test_mode
from codelab_relay.services.usage_service import UsageService
from codelab_relay.settings import RelaySettings
from codelab_relay import dependencies as relay_dependencies
from codelab_relay.infra.httpx_upstream_client import pin_to_address
from core.dependencies import container, get_auth_service, get_db_session
from core.exceptions import UnauthorizedError

PACKAGE_DIR = Path(__file__).resolve().parent.parent / "codelab_relay"
PROVIDERS_FILE = PACKAGE_DIR / "providers.json"
NOW = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
USER = SimpleNamespace(id=uuid.uuid4(), is_active=True, is_verified=True)
ORIGIN = "https://www.webneststudio.co.in"


def basic(user: str, secret: str = "secret") -> str:
    return "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode()


RZP_TEST = basic("rzp_test_1DP5mmOlF5G5ag", "thisisatestsecret")
RZP_LIVE = basic("rzp_live_1DP5mmOlF5G5ag", "thisisalivesecret")


def run(coroutine):
    return asyncio.run(coroutine)


def relay_settings(**overrides) -> RelaySettings:
    return RelaySettings(_env_file=None, **overrides)


def registry() -> JsonProviderRegistry:
    return JsonProviderRegistry(parse_providers(json.loads(PROVIDERS_FILE.read_text(encoding="utf-8"))))


def provider(provider_id: str):
    return next(p for p in registry()._providers if p.id == provider_id)


# ---- fakes -------------------------------------------------------------------


class FakeClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current


class FakeUpstream:
    def __init__(self, response: UpstreamResponse | None = None, error: Exception | None = None) -> None:
        self.response = response or UpstreamResponse(200, {"content-type": "application/json"}, b'{"ok": true}')
        self.error = error
        self.calls: list[OutgoingRequest] = []

    async def send(self, request, max_response_bytes, timeout_seconds):
        self.calls.append(request)
        if self.error:
            raise self.error
        return self.response


class FakeResolver:
    def __init__(self, error: Exception | None = None, addresses=("104.18.12.1", "104.18.13.1")) -> None:
        self.error = error
        self.addresses = list(addresses)
        self.calls: list[str] = []

    async def resolve_public(self, host):
        self.calls.append(host)
        if self.error:
            raise self.error
        return self.addresses


class FakeDailyCounter:
    def __init__(self, per_day: int = 300, global_per_day: int = 5000) -> None:
        self.per_day = per_day
        self.global_per_day = global_per_day
        self.counts: dict = {}
        self.total = 0

    async def take(self, user_id, now):
        if self.counts.get(user_id, 0) >= self.per_day:
            raise errors.RateLimited(seconds_until_midnight(now), "day", f"{self.per_day} calls a day")
        if self.total >= self.global_per_day:
            raise errors.RateLimited(seconds_until_midnight(now), "global", "the CodeLab daily limit for all learners")
        self.counts[user_id] = self.counts.get(user_id, 0) + 1
        self.total += 1
        return self.per_day - self.counts[user_id]

    refund_error: Exception | None = None

    async def give_back(self, user_id, now):
        if self.refund_error:
            raise self.refund_error
        if self.counts.get(user_id, 0) > 0:
            self.counts[user_id] -= 1
            self.total -= 1

    async def remaining(self, user_id, now):
        return self.per_day - self.counts.get(user_id, 0)


class World:
    """A RelayService built from real rules and fakes for everything with I/O."""

    def __init__(self, upstream: FakeUpstream | None = None, resolver: FakeResolver | None = None,
                 daily: FakeDailyCounter | None = None, **setting_overrides) -> None:
        self.settings = relay_settings(**setting_overrides)
        self.clock = FakeClock()
        self.upstream = upstream or FakeUpstream()
        self.resolver = resolver or FakeResolver()
        self.daily = daily or FakeDailyCounter(self.settings.per_day, self.settings.global_per_day)
        self.minute = MemoryMinuteLimiter(self.settings.per_minute)
        self.limiter = CompositeLimiter(self.minute, self.daily)
        scanner = test_mode.LivePatternScanner(test_mode.GLOBAL_LIVE_PATTERNS + self.settings.extra_live_patterns)
        rules = test_mode.TestModeRuleFactory(test_mode.KeyPrefixRule(scanner), test_mode.SandboxHostRule(scanner))
        self.service = RelayService(self.settings, TargetParser(registry()), HeaderFilter(), rules, self.limiter,
                                    self.resolver, self.upstream, self.clock)

    def relay(self, host="api.razorpay.com", path="v1/orders", query="", headers=None, body=b"", method="POST",
              user_id=USER.id):
        headers = {"authorization": RZP_TEST, "content-type": "application/json"} if headers is None else headers
        return run(self.service.relay(user_id, method, host, path, query, headers, body))


@pytest.fixture
def world():
    return World()


@pytest.fixture
def client(world):
    def install(target: World) -> None:
        app.dependency_overrides[get_relay_service] = lambda: target.service
        app.dependency_overrides[get_usage_service] = lambda: UsageService(target.limiter, target.clock)
        app.dependency_overrides[get_provider_list_service] = lambda: ProviderListService(target.settings, registry())

    app.dependency_overrides[get_relay_user] = lambda: USER
    install(world)
    test_client = TestClient(app)
    test_client.install = install
    yield test_client
    app.dependency_overrides.clear()


def assert_relay_error(response, status: int, code: str, /, **extra) -> dict:
    assert response.status_code == status, response.text
    assert response.headers["x-webnest-relay-error"] == code
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    body = response.json()
    assert body["code"] == code and isinstance(body["detail"], str) and body["detail"]
    for key, value in extra.items():
        assert body[key] == value, (key, body)
    return body


RAZORPAY_ORDER = "/api/codelab/relay/api.razorpay.com/v1/orders"


# ---- TargetParser ------------------------------------------------------------


@pytest.mark.parametrize("path", [
    "v1/../admin", "v1/./orders", "../v1/orders", "v1/..", "v1//orders", "v1\\orders", "v1/%2e%2e/admin",
    "v1/%2E/x", "v1%2forders", "v1/%2Forders", "v1/%5corders", "v1/orders%00", "v1/or ders", "v1/or\tders",
    "v1/ordérs", "v1/orders\r\n", "v1/" + "a" * 2100,
    # A decoded %3F / %23 would smuggle a query or fragment past the query checks.
    "v1/charges?x=sk_live_abc", "v1/orders#frag", "v1/orders%3Fx=1", "v1/orders%23f",
])
def test_parser_rejects_path_tricks(path):
    with pytest.raises(errors.BadTarget):
        TargetParser(registry()).parse("api.razorpay.com", path, "")


@pytest.mark.parametrize("host", [
    "API.razorpay.com", "api.razorpay.com:443", "user@api.razorpay.com", "127.0.0.1", "169.254.169.254",
    "10.0.0.5", "localhost", "api.razorpay.com.", ".api.razorpay.com", "api_razorpay.com", "api.razorpay.com/x",
    "ápi.razorpay.com", "", "a" * 254 + ".com",
])
def test_parser_rejects_malformed_hosts(host):
    with pytest.raises(errors.BadTarget):
        TargetParser(registry()).parse(host, "v1/orders", "")


def test_parser_rejects_control_characters_in_the_query():
    with pytest.raises(errors.BadTarget):
        TargetParser(registry()).parse("api.razorpay.com", "v1/orders", "a=1 2")


def test_parser_refuses_unknown_and_live_hosts():
    for host in ("api-m.paypal.com", "metadata.google.internal", "evil.example.com"):
        with pytest.raises(errors.HostNotAllowed) as caught:
            TargetParser(registry()).parse(host, "v1/x", "")
        assert caught.value.extra == {"host": host}


def test_parser_refuses_a_disabled_provider():
    with pytest.raises(errors.ProviderDisabled) as caught:
        TargetParser(registry()).parse("sandbox.cashfree.com", "pg/orders", "")
    assert caught.value.extra == {"provider": "cashfree"}


def test_parser_enforces_path_prefixes():
    for path in ("admin", "v3/orders", "v1", ""):
        with pytest.raises(errors.PathNotAllowed):
            TargetParser(registry()).parse("api.razorpay.com", path, "")


def test_parser_builds_an_https_url_with_the_query_unchanged():
    found, url = TargetParser(registry()).parse("api.stripe.com", "v1/customers", "limit=3&expand[]=data.x")
    assert found.id == "stripe"
    assert url == "https://api.stripe.com/v1/customers?limit=3&expand[]=data.x"
    assert TargetParser(registry()).parse("api.razorpay.com", "v1/orders", "")[1] == "https://api.razorpay.com/v1/orders"


# ---- test-mode rules -----------------------------------------------------------


def scanner(*extra):
    return test_mode.LivePatternScanner(test_mode.GLOBAL_LIVE_PATTERNS + extra)


def key_rule(provider_id, headers, query="", body=b""):
    test_mode.KeyPrefixRule(scanner()).check(provider(provider_id), headers, query, body)


def test_key_prefix_accepts_bearer_and_basic_test_keys():
    key_rule("stripe", {"authorization": "Bearer sk_test_51PqExampleKey"})
    key_rule("stripe", {"authorization": "Bearer rk_test_51PqExampleKey"})
    key_rule("stripe", {"authorization": basic("sk_test_51PqExampleKey", "")})
    key_rule("razorpay", {"authorization": RZP_TEST})


@pytest.mark.parametrize("headers,query,body", [
    ({"authorization": RZP_LIVE}, "", b""),
    ({"authorization": "Bearer sk_live_51PqExampleKey"}, "", b""),
    ({"authorization": "bearer SK_LIVE_51PqExampleKey"}, "", b""),
    ({"authorization": RZP_TEST, "accept": "rzp_live_abc"}, "", b""),
    ({"authorization": RZP_TEST}, "key=rzp_live_abc", b""),
    ({"authorization": RZP_TEST}, "key=rzp%5Flive%5Fabc", b""),
    ({"authorization": RZP_TEST}, "", b'{"notes": {"key": "rzp_live_abc"}}'),
    ({"authorization": RZP_TEST}, "", b"key=sk%5Flive%5Fabc"),
    ({"authorization": RZP_TEST}, "", b"publishable=pk_live_abc"),
])
def test_key_prefix_blocks_live_patterns_anywhere(headers, query, body):
    with pytest.raises(errors.LiveKeyBlocked) as caught:
        key_rule("razorpay", headers, query, body)
    assert caught.value.extra == {"provider": "razorpay", "hint": "Use a test key that starts with rzp_test_"}


@pytest.mark.parametrize("authorization", [
    None, "", "Bearer", "Bearer ", "Basic !!!notbase64", "Basic " + base64.b64encode(b"\xff\xfe:x").decode(),
    "Token rzp_test_abc", basic("rzp_test_x")[:-3], "Bearer sk_test_abc", basic("pk_test_x"),
])
def test_key_prefix_requires_a_recognisable_test_key(authorization):
    headers = {} if authorization is None else {"authorization": authorization}
    with pytest.raises(errors.TestKeyRequired) as caught:
        key_rule("razorpay", headers)
    assert caught.value.extra["provider"] == "razorpay"


def test_stripe_publishable_test_key_is_not_a_secret_key():
    with pytest.raises(errors.TestKeyRequired):
        key_rule("stripe", {"authorization": "Bearer pk_test_abc"})


def test_extra_live_patterns_from_settings_are_blocked():
    rule = test_mode.KeyPrefixRule(scanner("prod_key_"))
    with pytest.raises(errors.LiveKeyBlocked):
        rule.check(provider("razorpay"), {"authorization": RZP_TEST}, "", b"PROD_KEY_123")


def test_sandbox_host_rule_blocks_live_patterns_and_passes_sandbox_calls():
    rule = test_mode.SandboxHostRule(scanner())
    rule.check(provider("paypal"), {"authorization": basic("AeA1QIZ", "EB2hhP")}, "", b"grant_type=client_credentials")
    with pytest.raises(errors.LiveKeyBlocked) as caught:
        rule.check(provider("paypal"), {"authorization": "Bearer sk_live_x"}, "", b"")
    assert caught.value.extra["provider"] == "paypal"


class ExplodingHeaders(dict):
    def values(self):
        raise RuntimeError("boom")


def test_rules_fail_closed_on_internal_errors():
    with pytest.raises(errors.LiveKeyBlocked):
        scanner().find(ExplodingHeaders(), "", b"")
    with pytest.raises(errors.LiveKeyBlocked):
        test_mode.SandboxHostRule(scanner()).check(provider("paypal"), ExplodingHeaders(), "", b"")
    with pytest.raises(errors.LiveKeyBlocked):
        test_mode.KeyPrefixRule(scanner()).check(provider("razorpay"), ExplodingHeaders(), "", b"")


def test_unknown_test_mode_rule_never_relays():
    factory = test_mode.TestModeRuleFactory(object(), object())
    odd = SimpleNamespace(id="odd", test_mode={"rule": "trust_me"})
    with pytest.raises(errors.ProviderDisabled):
        factory.for_provider(odd)


# ---- HeaderFilter --------------------------------------------------------------


def test_outgoing_headers_keep_only_the_provider_allowlist():
    incoming = {
        "authorization": RZP_TEST, "content-type": "application/json", "accept": "application/json",
        "x-webnest-authorization": "Bearer webnest-token", "cookie": "session=1", "host": "evil",
        "origin": ORIGIN, "referer": ORIGIN, "x-forwarded-for": "1.2.3.4", "x-real-ip": "1.2.3.4",
        "user-agent": "Pyodide", "content-length": "10", "stripe-version": "2024-01-01", "x-custom": "1",
    }
    outgoing = HeaderFilter().outgoing(provider("razorpay"), incoming)
    assert outgoing == {"authorization": RZP_TEST, "content-type": "application/json", "accept": "application/json",
                        "user-agent": "WebNest-CodeLab-Relay/1.0"}


def test_outgoing_header_values_with_line_breaks_are_rejected():
    for value in ("a\r\nX-Injected: 1", "a\nb", "a\x00"):
        with pytest.raises(errors.BadTarget):
            HeaderFilter().outgoing(provider("razorpay"), {"authorization": value})


def test_incoming_headers_drop_cookies_redirects_and_transport_headers():
    upstream = {
        "content-type": "application/json", "request-id": "req_1", "set-cookie": "a=b", "location": "https://x",
        "content-encoding": "gzip", "content-length": "5", "strict-transport-security": "max-age=1",
        "alt-svc": "h3", "server": "nginx", "transfer-encoding": "chunked",
    }
    assert HeaderFilter().incoming(provider("stripe"), upstream) == {"content-type": "application/json",
                                                                     "request-id": "req_1"}
    assert HeaderFilter().incoming(provider("razorpay"), upstream) == {"content-type": "application/json"}


# ---- RelayService --------------------------------------------------------------


def test_successful_call_reaches_the_provider_with_filtered_headers(world):
    response, found, remaining = world.relay(
        headers={"authorization": RZP_TEST, "content-type": "application/json",
                 "x-webnest-authorization": "Bearer webnest-token", "cookie": "a=b"},
        body=b'{"amount": 50000}')
    assert (response.status, found.id, remaining) == (200, "razorpay", 29)
    sent = world.upstream.calls[0]
    assert (sent.method, sent.url, sent.body) == ("POST", "https://api.razorpay.com/v1/orders", b'{"amount": 50000}')
    assert "x-webnest-authorization" not in sent.headers and "cookie" not in sent.headers
    assert sent.headers["user-agent"] == "WebNest-CodeLab-Relay/1.0"
    assert world.resolver.calls == ["api.razorpay.com"]
    assert sent.connect_address == "104.18.12.1"   # pinned to the address the resolver checked


@pytest.mark.parametrize("kwargs,error", [
    ({"host": "api-m.paypal.com"}, errors.HostNotAllowed),
    ({"path": "v1/../admin"}, errors.BadTarget),
    ({"path": "admin"}, errors.PathNotAllowed),
    ({"headers": {"authorization": RZP_LIVE}}, errors.LiveKeyBlocked),
    ({"headers": {}}, errors.TestKeyRequired),
    ({"method": "TRACE"}, errors.MethodNotAllowed),
    ({"headers": {"authorization": "a\r\nb"}}, errors.BadTarget),
])
def test_refused_calls_cost_nothing_upstream(world, kwargs, error):
    with pytest.raises(error):
        world.relay(**kwargs)
    assert world.upstream.calls == [] and world.resolver.calls == []
    assert run(world.minute.remaining(USER.id, NOW)) == 30 and world.daily.total == 0


def test_rate_limit_is_checked_before_dns_and_network():
    world = World(per_minute=1)
    world.relay()
    with pytest.raises(errors.RateLimited) as caught:
        world.relay()
    assert caught.value.extra["scope"] == "minute" and 1 <= caught.value.extra["retry_after"] <= 60
    assert caught.value.headers == {"Retry-After": str(caught.value.extra["retry_after"])}
    assert len(world.upstream.calls) == 1 and len(world.resolver.calls) == 1


def test_kill_switch_and_blocked_users_are_refused_first():
    with pytest.raises(errors.RelayDisabled):
        World(enabled=False).relay()
    blocked = World(blocked_user_ids_text=f"{USER.id}, not-a-uuid")
    with pytest.raises(errors.UserBlocked):
        blocked.relay()
    blocked.relay(user_id=uuid.uuid4())   # other learners are unaffected


def test_oversized_body_is_refused():
    with pytest.raises(errors.PayloadTooLarge) as caught:
        World(max_request_bytes=10).relay(body=b"x" * 11)
    assert caught.value.extra == {"limit_bytes": 10}


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirects_are_never_followed_or_returned(status):
    world = World(upstream=FakeUpstream(UpstreamResponse(status, {"location": "http://169.254.169.254/"}, b"")))
    with pytest.raises(errors.RedirectBlocked) as caught:
        world.relay()
    assert caught.value.extra == {"provider": "razorpay", "status": status}


def test_upstream_failures_are_reported_with_the_provider_name():
    world = World(upstream=FakeUpstream(error=errors.UpstreamTimeout("api.razorpay.com", 20)))
    with pytest.raises(errors.UpstreamTimeout) as caught:
        world.relay()
    assert caught.value.message == "Razorpay did not answer within 20 seconds."
    assert caught.value.extra == {"provider": "razorpay", "timeout_seconds": 20}

    world = World(resolver=FakeResolver(errors.UpstreamUnavailable("api.razorpay.com")))
    with pytest.raises(errors.UpstreamUnavailable) as caught:
        world.relay()
    assert caught.value.extra == {"provider": "razorpay"} and world.upstream.calls == []

    world = World(upstream=FakeUpstream(error=RuntimeError("socket exploded with secret rzp_test_x")))
    with pytest.raises(errors.UpstreamUnavailable) as caught:
        world.relay()
    assert "secret" not in caught.value.message


@pytest.mark.parametrize("make_world", [
    lambda: World(upstream=FakeUpstream(error=errors.UpstreamTimeout("api.razorpay.com", 20))),
    lambda: World(upstream=FakeUpstream(error=errors.UpstreamUnavailable("api.razorpay.com"))),
    lambda: World(upstream=FakeUpstream(error=errors.ResponseTooLarge(10))),
    lambda: World(upstream=FakeUpstream(error=RuntimeError("unexpected"))),
    lambda: World(upstream=FakeUpstream(UpstreamResponse(302, {}, b""))),
    lambda: World(resolver=FakeResolver(errors.UpstreamUnavailable("api.razorpay.com"))),
], ids=["timeout", "unavailable", "too-large", "unexpected", "redirect", "dns"])
def test_relay_failures_give_the_day_quota_back(make_world):
    world = make_world()
    for _ in range(3):
        with pytest.raises(errors.RelayError):
            world.relay()
    assert world.daily.total == 0 and world.daily.counts[USER.id] == 0
    # The minute slots stay used, so retry storms are still throttled.
    assert run(world.minute.remaining(USER.id, NOW)) == 27


def test_provider_answers_are_counted_even_when_they_are_errors():
    world = World(upstream=FakeUpstream(UpstreamResponse(401, {}, b"{}")))
    world.relay()
    assert world.daily.total == 1


def test_a_failed_refund_never_replaces_the_learners_error():
    world = World(upstream=FakeUpstream(error=errors.UpstreamTimeout("api.razorpay.com", 20)))
    world.daily.refund_error = RuntimeError("database down")
    with pytest.raises(errors.UpstreamTimeout):
        world.relay()


# ---- limiters ------------------------------------------------------------------


def test_minute_window_slides():
    limiter = MemoryMinuteLimiter(2)
    assert run(limiter.take(USER.id, NOW)) == 1
    assert run(limiter.take(USER.id, NOW + timedelta(seconds=30))) == 0
    with pytest.raises(errors.RateLimited) as caught:
        run(limiter.take(USER.id, NOW + timedelta(seconds=45)))
    assert caught.value.extra["retry_after"] == 15
    assert run(limiter.take(USER.id, NOW + timedelta(seconds=61))) == 0
    assert run(limiter.remaining(uuid.uuid4(), NOW)) == 2


def test_day_refusal_does_not_use_up_the_minute():
    daily = FakeDailyCounter(per_day=1)
    limiter = CompositeLimiter(MemoryMinuteLimiter(30), daily)
    assert run(limiter.consume(USER.id, NOW)) == 29
    with pytest.raises(errors.RateLimited) as caught:
        run(limiter.consume(USER.id, NOW))
    assert caught.value.extra == {"retry_after": 12 * 3600, "scope": "day"}
    assert run(limiter.remaining(USER.id, NOW)) == {
        "minute_remaining": 29, "day_remaining": 0, "day_resets_at": datetime(2026, 10, 25, tzinfo=timezone.utc)}


def test_global_cap_is_reported_with_its_own_scope():
    limiter = CompositeLimiter(MemoryMinuteLimiter(30), FakeDailyCounter(global_per_day=0))
    with pytest.raises(errors.RateLimited) as caught:
        run(limiter.consume(USER.id, NOW))
    assert caught.value.extra["scope"] == "global"


# ---- router: every row of the contract table -------------------------------------


def test_sample_a_success_passes_the_provider_response_through(client, world):
    order = {"id": "order_RXq3sYf0nV2kTz", "entity": "order", "amount": 50000, "status": "created"}
    world.upstream.response = UpstreamResponse(
        200, {"content-type": "application/json", "set-cookie": "a=b", "server": "razorpay"}, json.dumps(order).encode())
    response = client.post(RAZORPAY_ORDER, headers={"Authorization": RZP_TEST, "Content-Type": "application/json",
                                                    "X-WebNest-Authorization": "Bearer webnest", "Cookie": "s=1"},
                           content=b'{"amount": 50000, "currency": "INR", "receipt": "rcpt_1001"}')
    assert response.status_code == 200 and response.json() == order
    assert response.headers["x-webnest-relay-provider"] == "razorpay"
    assert response.headers["x-webnest-relay-remaining"] == "29"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-type"] == "application/json"
    assert "x-webnest-relay-error" not in response.headers
    assert "set-cookie" not in response.headers and "server" not in response.headers
    sent = world.upstream.calls[0]
    assert sent.url == "https://api.razorpay.com/v1/orders"
    assert sent.body == b'{"amount": 50000, "currency": "INR", "receipt": "rcpt_1001"}'
    assert set(sent.headers) == {"authorization", "content-type", "accept", "user-agent"}   # TestClient adds Accept


def test_sample_b_upstream_errors_pass_through_without_the_relay_error_header(client, world):
    body = {"error": {"code": "BAD_REQUEST_ERROR", "description": "Authentication failed"}}
    world.upstream.response = UpstreamResponse(401, {"content-type": "application/json"}, json.dumps(body).encode())
    response = client.post(RAZORPAY_ORDER, headers={"Authorization": RZP_TEST}, content=b"{}")
    assert response.status_code == 401 and response.json() == body
    assert "x-webnest-relay-error" not in response.headers
    assert response.headers["x-webnest-relay-provider"] == "razorpay"


def test_sample_c_live_key(client):
    response = client.post(RAZORPAY_ORDER, headers={"Authorization": RZP_LIVE}, content=b"{}")
    body = assert_relay_error(response, 403, "relay_live_key_blocked", provider="razorpay",
                              hint="Use a test key that starts with rzp_test_")
    assert body["detail"] == "Live keys are blocked in CodeLab. Real payments are never made from the playground."


def test_sample_d_stripe_form_body_and_headers(client, world):
    response = client.post(
        "/api/codelab/relay/api.stripe.com/v1/payment_intents",
        headers={"Authorization": "Bearer sk_test_51PqExampleKey", "Content-Type": "application/x-www-form-urlencoded",
                 "Idempotency-Key": "6f1c2a9e", "Stripe-Version": "2024-06-20"},
        content=b"amount=50000&currency=inr&automatic_payment_methods[enabled]=true")
    assert response.status_code == 200
    sent = world.upstream.calls[0]
    assert sent.headers["idempotency-key"] == "6f1c2a9e" and sent.headers["stripe-version"] == "2024-06-20"
    assert sent.body == b"amount=50000&currency=inr&automatic_payment_methods[enabled]=true"


def test_sample_e_paypal_sandbox_works_and_live_host_is_refused(client, world):
    headers = {"Authorization": basic("AeA1QIZXiflr1", "EB2hhP7yZyVa3x"),
               "Content-Type": "application/x-www-form-urlencoded"}
    ok = client.post("/api/codelab/relay/api-m.sandbox.paypal.com/v1/oauth2/token", headers=headers,
                     content=b"grant_type=client_credentials")
    assert ok.status_code == 200 and world.upstream.calls[0].url == "https://api-m.sandbox.paypal.com/v1/oauth2/token"
    live = client.post("/api/codelab/relay/api-m.paypal.com/v1/oauth2/token", headers=headers,
                       content=b"grant_type=client_credentials")
    body = assert_relay_error(live, 403, "relay_host_not_allowed", host="api-m.paypal.com")
    assert body["detail"] == ("api-m.paypal.com is not available through the CodeLab relay. "
                              "Use the provider's sandbox host.")


def test_sample_f_login_required_without_a_webnest_token(client):
    app.dependency_overrides.pop(get_relay_user)
    response = client.post(RAZORPAY_ORDER, headers={"Authorization": RZP_TEST}, content=b"{}")
    body = assert_relay_error(response, 401, "relay_login_required")
    assert body["detail"] == "Log in to WebNest to call external APIs from the playground."


def test_sample_g_rate_limited(client):
    limited = World(per_minute=1)
    client.install(limited)
    assert client.post(RAZORPAY_ORDER, headers={"Authorization": RZP_TEST}, content=b"{}").status_code == 200
    response = client.post(RAZORPAY_ORDER, headers={"Authorization": RZP_TEST}, content=b"{}")
    body = assert_relay_error(response, 429, "relay_rate_limited", scope="minute")
    assert response.headers["retry-after"] == str(body["retry_after"])
    assert body["detail"] == f"Relay limit reached: 1 calls a minute. Try again in {body['retry_after']} seconds."


def test_sample_h_upstream_timeout(client):
    client.install(World(upstream=FakeUpstream(error=errors.UpstreamTimeout("api.razorpay.com", 20))))
    response = client.post(RAZORPAY_ORDER, headers={"Authorization": RZP_TEST}, content=b"{}")
    body = assert_relay_error(response, 504, "relay_upstream_timeout", provider="razorpay", timeout_seconds=20)
    assert body["detail"] == "Razorpay did not answer within 20 seconds."


def test_sample_i_bad_target(client, world):
    response = client.get("/api/codelab/relay/api.razorpay.com/v1/%2e%2e/%2e%2e/admin",
                          headers={"Authorization": RZP_TEST})
    body = assert_relay_error(response, 400, "relay_bad_target")
    assert body["detail"] == "The relay URL is not valid."
    assert world.upstream.calls == []


def test_an_encoded_question_mark_cannot_smuggle_a_query(client, world):
    response = client.get("/api/codelab/relay/api.stripe.com/v1/charges%3Fx=sk_live_abc",
                          headers={"Authorization": "Bearer sk_test_ok"})
    assert_relay_error(response, 400, "relay_bad_target")
    response = client.get("/api/codelab/relay/api.stripe.com/v1/charges%23frag?limit=1",
                          headers={"Authorization": "Bearer sk_test_ok"})
    assert_relay_error(response, 400, "relay_bad_target")
    assert world.upstream.calls == []


@pytest.mark.parametrize("url,headers,status,code,extra", [
    ("/api/codelab/relay/sandbox.cashfree.com/pg/orders", {}, 403, "relay_provider_disabled", {"provider": "cashfree"}),
    ("/api/codelab/relay/api.razorpay.com/admin/users", {"Authorization": RZP_TEST}, 403, "relay_path_not_allowed",
     {"provider": "razorpay"}),
    ("/api/codelab/relay/api.razorpay.com/v1/orders", {}, 403, "relay_test_key_required",
     {"provider": "razorpay", "hint": "Use a test key that starts with rzp_test_"}),
    ("/api/codelab/relay/169.254.169.254/latest/meta-data", {}, 400, "relay_bad_target", {}),
    ("/api/codelab/relay/api.razorpay.com:8443/v1/orders", {"Authorization": RZP_TEST}, 400, "relay_bad_target", {}),
])
def test_refusals_return_the_documented_error(client, world, url, headers, status, code, extra):
    assert_relay_error(client.post(url, headers=headers, content=b"{}"), status, code, **extra)
    assert world.upstream.calls == []


def test_user_blocked(client):
    client.install(World(blocked_user_ids_text=str(USER.id)))
    assert_relay_error(client.post(RAZORPAY_ORDER, headers={"Authorization": RZP_TEST}), 403, "relay_user_blocked")


@pytest.mark.parametrize("method", ["TRACE", "OPTIONS", "HEAD"])
def test_other_methods_are_refused(client, method):
    response = client.request(method, RAZORPAY_ORDER, headers={"Authorization": RZP_TEST})
    assert response.status_code == 405
    assert response.headers["x-webnest-relay-error"] == "relay_method_not_allowed"
    if method != "HEAD":
        assert response.json()["code"] == "relay_method_not_allowed"


def test_payload_too_large_streams_with_a_cap(client):
    app.dependency_overrides[get_body_reader] = lambda: BodyReader(100)
    response = client.post(RAZORPAY_ORDER, headers={"Authorization": RZP_TEST}, content=b"x" * 101)
    assert_relay_error(response, 413, "relay_payload_too_large", limit_bytes=100)

    def chunked():
        yield b"x" * 60
        yield b"x" * 60

    response = client.post(RAZORPAY_ORDER, headers={"Authorization": RZP_TEST}, content=chunked())
    assert_relay_error(response, 413, "relay_payload_too_large", limit_bytes=100)


@pytest.mark.parametrize("error,status,code,extra", [
    (errors.UpstreamUnavailable("api.razorpay.com"), 502, "relay_upstream_unavailable", {"provider": "razorpay"}),
    (errors.ResponseTooLarge(2097152), 502, "relay_response_too_large", {"limit_bytes": 2097152}),
])
def test_upstream_failures(client, error, status, code, extra):
    client.install(World(upstream=FakeUpstream(error=error)))
    assert_relay_error(client.post(RAZORPAY_ORDER, headers={"Authorization": RZP_TEST}), status, code, **extra)


def test_redirect_blocked(client):
    client.install(World(upstream=FakeUpstream(UpstreamResponse(302, {"location": "https://evil.example"}, b""))))
    response = client.post(RAZORPAY_ORDER, headers={"Authorization": RZP_TEST})
    assert_relay_error(response, 502, "relay_redirect_blocked", provider="razorpay", status=302)
    assert "location" not in response.headers


def test_relay_disabled(client):
    client.install(World(enabled=False))
    assert_relay_error(client.post(RAZORPAY_ORDER, headers={"Authorization": RZP_TEST}), 503, "relay_disabled")


def test_providers_lists_enabled_providers_only(client):
    response = client.get("/api/codelab/relay/providers")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=300"
    body = response.json()
    assert body["enabled"] is True and body["relay_path"] == "/api/codelab/relay"
    assert body["limits"] == {"per_minute": 30, "per_day": 300, "max_request_bytes": 1048576,
                              "max_response_bytes": 2097152, "timeout_seconds": 20}
    assert [p["id"] for p in body["providers"]] == ["razorpay", "stripe", "paypal"]
    assert body["providers"][0] == {"id": "razorpay", "name": "Razorpay", "hosts": ["api.razorpay.com"],
                                    "test_mode": "key_prefix", "test_key_hint": "Use a test key that starts with rzp_test_",
                                    "docs_url": "https://razorpay.com/docs/api/"}
    assert body["providers"][2]["test_mode"] == "sandbox_host"


def test_providers_when_switched_off(client):
    client.install(World(enabled=False))
    body = client.get("/api/codelab/relay/providers").json()
    assert body["enabled"] is False and body["providers"] == []


def test_usage(client):
    client.post(RAZORPAY_ORDER, headers={"Authorization": RZP_TEST})
    response = client.get("/api/codelab/relay/usage")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json() == {"minute_remaining": 29, "day_remaining": 299, "day_resets_at": "2026-10-25T00:00:00Z"}


# ---- login (X-WebNest-Authorization) ---------------------------------------------


class FakeAuth:
    def __init__(self, user=None) -> None:
        self.user = user
        self.tokens: list[str] = []

    async def get_current_user(self, token):
        self.tokens.append(token)
        if self.user is None:
            raise UnauthorizedError("Invalid or expired access token")
        return self.user


class FakeSession:
    def __init__(self) -> None:
        self.closed = 0

    async def close(self):
        self.closed += 1


@pytest.fixture
def real_login(client):
    app.dependency_overrides.pop(get_relay_user)
    auth = FakeAuth()
    auth.session = FakeSession()
    app.dependency_overrides[get_auth_service] = lambda: auth
    app.dependency_overrides[get_db_session] = lambda: auth.session
    return auth


@pytest.mark.parametrize("headers", [
    {},
    {"X-WebNest-Authorization": "Bearer"},
    {"X-WebNest-Authorization": "Token abc"},
    {"X-WebNest-Authorization": "Bearer expired"},
    {"Authorization": "Bearer webnest-token"},   # the provider's header is never our login
])
def test_login_is_read_from_the_webnest_header_only(client, real_login, headers):
    assert_relay_error(client.get("/api/codelab/relay/usage", headers=headers), 401, "relay_login_required")
    assert all(token == "expired" for token in real_login.tokens)


def test_inactive_accounts_are_refused(client, real_login):
    real_login.user = SimpleNamespace(id=uuid.uuid4(), is_active=False, is_verified=True)
    response = client.get("/api/codelab/relay/usage", headers={"X-WebNest-Authorization": "Bearer ok"})
    assert_relay_error(response, 401, "relay_login_required")


def test_unverified_accounts_follow_the_login_rule(client, real_login, monkeypatch):
    real_login.user = SimpleNamespace(id=uuid.uuid4(), is_active=True, is_verified=False)
    headers = {"X-WebNest-Authorization": "Bearer ok", "Authorization": RZP_TEST}
    # Email off (the default): login never requires verification, so neither does the relay.
    monkeypatch.setattr(container.settings, "email_enabled", False)
    assert client.post(RAZORPAY_ORDER, headers=headers, content=b"{}").status_code == 200
    # Email on: an unverified account can't log in, and can't use the relay.
    monkeypatch.setattr(container.settings, "email_enabled", True)
    assert_relay_error(client.post(RAZORPAY_ORDER, headers=headers, content=b"{}"), 401, "relay_login_required")


def test_valid_login_is_accepted_and_never_forwarded(client, world, real_login):
    real_login.user = USER
    response = client.post(RAZORPAY_ORDER, headers={"X-WebNest-Authorization": "Bearer good-token",
                                                    "Authorization": RZP_TEST}, content=b"{}")
    assert response.status_code == 200 and real_login.tokens == ["good-token"]
    assert "x-webnest-authorization" not in world.upstream.calls[0].headers
    assert world.upstream.calls[0].headers["authorization"] == RZP_TEST
    # The login session is handed back before the upstream call.
    assert real_login.session.closed == 1


class SpyBodyReader:
    def __init__(self) -> None:
        self.reads = 0

    async def read(self, request):
        self.reads += 1
        return b""


@pytest.mark.parametrize("method", ["TRACE", "OPTIONS", "HEAD"])
def test_other_methods_get_405_before_login_or_body_reading(client, real_login, method):
    reader = SpyBodyReader()
    app.dependency_overrides[get_body_reader] = lambda: reader
    response = client.request(method, RAZORPAY_ORDER)   # no WebNest login at all
    assert response.status_code == 405
    assert response.headers["x-webnest-relay-error"] == "relay_method_not_allowed"
    assert real_login.tokens == [] and reader.reads == 0


def test_switched_off_relay_answers_503_before_login(client, real_login, monkeypatch):
    monkeypatch.setattr(relay_dependencies, "relay_settings", relay_settings(enabled=False))
    assert_relay_error(client.post(RAZORPAY_ORDER, content=b"{}"), 503, "relay_disabled")
    assert real_login.tokens == []


# ---- CORS (section 2.4) ------------------------------------------------------------


def preflight(path, origin=ORIGIN, headers="x-webnest-authorization,authorization,content-type"):
    return TestClient(app).options(path, headers={"Origin": origin, "Access-Control-Request-Method": "POST",
                                                  "Access-Control-Request-Headers": headers})


def test_relay_preflight_allows_listed_origins_and_headers_without_credentials():
    response = preflight(RAZORPAY_ORDER)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert "access-control-allow-credentials" not in response.headers
    assert response.headers["access-control-max-age"] == "600"
    allowed = {h.strip().lower() for h in response.headers["access-control-allow-headers"].split(",")}
    assert {"x-webnest-authorization", "authorization", "idempotency-key", "stripe-version",
            "paypal-request-id"} <= allowed and "*" not in allowed
    assert "x-client-secret" not in allowed   # Cashfree is disabled
    for origin in ("http://127.0.0.1:5173", "http://localhost:4173"):
        assert preflight(RAZORPAY_ORDER, origin=origin).status_code == 200


def test_relay_preflight_refuses_other_origins_and_headers():
    response = preflight(RAZORPAY_ORDER, origin="https://evil.example")
    assert response.status_code == 400 and "access-control-allow-origin" not in response.headers
    assert preflight(RAZORPAY_ORDER, headers="x-webnest-authorization,x-evil").status_code == 400


def test_relay_responses_expose_the_relay_headers(client):
    response = client.post(RAZORPAY_ORDER, headers={"Origin": ORIGIN, "Authorization": RZP_TEST})
    assert response.headers["access-control-allow-origin"] == ORIGIN
    exposed = {h.strip().lower() for h in response.headers["access-control-expose-headers"].split(",")}
    assert {"x-webnest-relay-error", "x-webnest-relay-provider", "x-webnest-relay-remaining", "retry-after",
            "request-id", "paypal-debug-id"} <= exposed
    refused = client.post(RAZORPAY_ORDER, headers={"Origin": ORIGIN, "Authorization": RZP_LIVE})
    assert refused.headers["access-control-allow-origin"] == ORIGIN


def test_other_routes_keep_the_site_wide_cors_policy():
    response = preflight("/api/github/status", headers="authorization")
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-credentials") == "true"


# ---- HttpxUpstreamClient (httpx.MockTransport) -----------------------------------------


def upstream_with(handler, built: list | None = None) -> HttpxUpstreamClient:
    def make():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False, trust_env=False)
        if built is not None:
            built.append(client)
        return client

    return HttpxUpstreamClient(make)


def outgoing(url="https://api.razorpay.com/v1/orders?x=1") -> OutgoingRequest:
    return OutgoingRequest("POST", url, {"authorization": RZP_TEST, "user-agent": "WebNest-CodeLab-Relay/1.0"}, b"{}")


def test_upstream_client_returns_status_headers_and_body():
    seen = {}

    def handler(request):
        seen.update(url=str(request.url), auth=request.headers["authorization"], body=request.content)
        return httpx.Response(201, headers={"Content-Type": "application/json", "Request-Id": "r1"}, content=b'{"a":1}')

    response = run(upstream_with(handler).send(outgoing(), 1000, 5))
    assert response == UpstreamResponse(201, {"content-type": "application/json", "request-id": "r1",
                                              "content-length": "7"}, b'{"a":1}')
    assert seen == {"url": "https://api.razorpay.com/v1/orders?x=1", "auth": RZP_TEST, "body": b"{}"}


def test_upstream_client_never_follows_redirects():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})

    assert run(upstream_with(handler).send(outgoing(), 1000, 5)).status == 302
    assert len(calls) == 1


def test_upstream_client_timeouts():
    def raises_timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(errors.UpstreamTimeout) as caught:
        run(upstream_with(raises_timeout).send(outgoing(), 1000, 5))
    assert caught.value.extra == {"provider": "api.razorpay.com", "timeout_seconds": 5}

    async def hangs(request):
        await asyncio.sleep(5)
        return httpx.Response(200)

    with pytest.raises(errors.UpstreamTimeout):
        run(upstream_with(hangs).send(outgoing(), 1000, 0.05))


def test_upstream_client_connection_errors():
    def refuses(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(errors.UpstreamUnavailable) as caught:
        run(upstream_with(refuses).send(outgoing(), 1000, 5))
    assert caught.value.extra == {"provider": "api.razorpay.com"}


def test_upstream_client_caps_the_response_body():
    def big(request):
        return httpx.Response(200, content=b"x" * 1001)

    with pytest.raises(errors.ResponseTooLarge) as caught:
        run(upstream_with(big).send(outgoing(), 1000, 5))
    assert caught.value.extra == {"limit_bytes": 1000}


def test_upstream_client_connects_to_the_checked_address_with_the_real_host_name():
    seen = {}

    def handler(request):
        seen.update(url=str(request.url), host=request.headers["host"],
                    sni=request.extensions.get("sni_hostname"))
        return httpx.Response(200, content=b"ok")

    pinned = OutgoingRequest("POST", "https://api.razorpay.com/v1/orders?x=1", {"authorization": RZP_TEST}, b"{}",
                             connect_address="104.18.12.1")
    assert run(upstream_with(handler).send(pinned, 1000, 5)).status == 200
    assert seen == {"url": "https://104.18.12.1/v1/orders?x=1", "host": "api.razorpay.com", "sni": "api.razorpay.com"}


def test_pinning_brackets_ipv6_and_rejects_non_addresses():
    v6 = OutgoingRequest("GET", "https://api.stripe.com/v1/x", {}, b"", connect_address="2606:4700::6812:c01")
    url, headers, extensions = pin_to_address(v6)
    assert url == "https://[2606:4700::6812:c01]/v1/x" and headers["host"] == "api.stripe.com"
    assert extensions == {"sni_hostname": "api.stripe.com"}
    assert pin_to_address(OutgoingRequest("GET", "https://a.b/x", {"k": "v"}, b""))[1:] == ({"k": "v"}, {})
    with pytest.raises(errors.UpstreamUnavailable):
        pin_to_address(OutgoingRequest("GET", "https://a.b/x", {}, b"", connect_address="evil.example"))


def test_upstream_client_is_rebuilt_after_close_and_on_a_new_event_loop():
    built = []
    client = upstream_with(lambda request: httpx.Response(200, content=b"ok"), built)

    async def send_then_close():
        assert (await client.send(outgoing(), 1000, 5)).status == 200
        await client.aclose()
        assert (await client.send(outgoing(), 1000, 5)).status == 200   # a second lifespan

    run(send_then_close())
    assert len(built) == 2
    run(client.send(outgoing(), 1000, 5))   # asyncio.run = a new event loop
    assert len(built) == 3
    run(client.aclose())
    run(client.aclose())   # closing twice is harmless


def test_shared_client_ignores_proxy_environment_and_redirects():
    from codelab_relay.infra.httpx_upstream_client import create_shared_httpx_client

    client = create_shared_httpx_client(5, 20)
    try:
        assert client.follow_redirects is False and client.trust_env is False
        assert client.timeout.connect == 5 and client.timeout.read == 20
    finally:
        run(client.aclose())


# ---- DnsHostResolver ---------------------------------------------------------------


def resolver_for(*addresses):
    async def lookup(host):
        return list(addresses)

    return DnsHostResolver(lookup)


@pytest.mark.parametrize("addresses", [
    ("127.0.0.1",), ("10.0.0.5",), ("169.254.169.254",), ("192.168.1.10",), ("172.16.0.1",), ("100.64.0.1",),
    ("0.0.0.0",), ("::1",), ("fe80::1%eth0",), ("fc00::1",), ("::ffff:10.0.0.1",), ("224.0.0.1",),
    ("104.18.12.1", "10.0.0.5"), (),
])
def test_resolver_refuses_non_public_answers(addresses):
    with pytest.raises(errors.UpstreamUnavailable):
        run(resolver_for(*addresses).resolve_public("api.razorpay.com"))


def test_resolver_returns_the_checked_public_answers():
    found = run(resolver_for("104.18.12.1", "2606:4700::6812:c01", "104.18.12.1").resolve_public("api.razorpay.com"))
    assert found == ["104.18.12.1", "2606:4700::6812:c01"]


def test_resolver_lookup_failures_are_unavailable():
    async def broken(host):
        raise OSError("no route")

    async def garbage(host):
        return [None]

    for lookup in (broken, garbage):
        with pytest.raises(errors.UpstreamUnavailable):
            run(DnsHostResolver(lookup).resolve_public("api.razorpay.com"))


# ---- registry ------------------------------------------------------------------------


def test_shipped_registry_loads_and_is_safe():
    shipped = load_provider_registry(str(PROVIDERS_FILE))
    hosts = [host for p in shipped._providers for host in p.hosts]
    assert len(hosts) == len(set(hosts))
    for found in shipped.all_enabled():
        for host in found.hosts:
            assert "live" not in host and host not in KNOWN_LIVE_HOSTS
        assert found.path_prefixes and all(prefix.startswith("/") for prefix in found.path_prefixes)
    assert shipped.for_host("sandbox.cashfree.com").enabled is False
    assert shipped.for_host("api-m.paypal.com") is None


def entry(**overrides) -> dict:
    base = json.loads(PROVIDERS_FILE.read_text(encoding="utf-8"))[0]
    base.update(overrides)
    return base


@pytest.mark.parametrize("entries", [
    [entry(), entry(id="other")],                                   # host twice
    [entry(), entry(hosts=["api.stripe.com"])],                    # id twice
    [entry(hosts=["API.razorpay.com"])],
    [entry(hosts=[])],
    [entry(hosts=["api.razorpay.com:443"])],
    [entry(path_prefixes=[])],
    [entry(path_prefixes=["v1/"])],
    [entry(test_mode={"rule": "trust_me"})],
    [entry(test_mode={"rule": "key_prefix"})],
    [entry(test_mode={"rule": "key_prefix", "allowed_prefixes": ["rzp_"]})],
    [entry(id="paypal", hosts=["api-m.paypal.com"], test_mode={"rule": "sandbox_host"})],
    [entry(hosts=["api.live.example.com"])],
    [entry(forward_request_headers=["authorization", "x-webnest-authorization"])],
    [entry(forward_request_headers=["cookie"])],
    [entry(forward_request_headers=["x-forwarded-for"])],
    [entry(enabled="yes")],
    [{"id": "razorpay"}],
    {"not": "a list"},
])
def test_registry_validation_refuses_bad_entries(entries):
    with pytest.raises(errors.RegistryInvalid):
        parse_providers(entries)


def test_disabled_live_host_may_be_listed_but_never_enabled():
    parse_providers([entry(id="paypal", enabled=False, hosts=["api-m.paypal.com"], test_mode={"rule": "sandbox_host"})])


def test_bad_registry_file_refuses_to_load(tmp_path):
    broken = tmp_path / "providers.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(errors.RegistryInvalid):
        load_provider_registry(str(broken))
    with pytest.raises(errors.RegistryInvalid):
        load_provider_registry(str(tmp_path / "missing.json"))


# ---- settings, logging --------------------------------------------------------------


def test_settings_ignore_unprefixed_keys_of_the_dotenv_file(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("CORS_ORIGINS=*\nPER_MINUTE=3\nENABLED=false\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    settings = RelaySettings()
    assert (settings.per_minute, settings.enabled) == (30, True)
    assert settings.cors_origin_list == ["https://www.webneststudio.co.in", "http://127.0.0.1:5173",
                                         "http://localhost:4173"]


def test_settings_parse_comma_separated_values():
    other = uuid.uuid4()
    settings = relay_settings(blocked_user_ids_text=f" {USER.id},junk,{other} ",
                              extra_live_patterns_text="Live_Secret_, prod_key_", cors_origins=f"{ORIGIN},*")
    assert settings.blocked_user_ids == {USER.id, other}
    assert settings.extra_live_patterns == ("live_secret_", "prod_key_")
    assert settings.cors_origin_list == [ORIGIN]


def test_parsed_settings_are_computed_once(caplog):
    settings = relay_settings(blocked_user_ids_text="junk")
    with caplog.at_level(logging.WARNING, logger="webnest.codelab_relay"):
        for _ in range(5):
            assert settings.blocked_user_ids == frozenset()
    assert sum("RELAY_BLOCKED_USER_IDS" in record.getMessage() for record in caplog.records) == 1


def test_settings_read_the_documented_environment_names(monkeypatch):
    monkeypatch.setenv("RELAY_ENABLED", "false")
    monkeypatch.setenv("RELAY_PER_MINUTE", "7")
    monkeypatch.setenv("RELAY_BLOCKED_USER_IDS", str(USER.id))
    monkeypatch.setenv("RELAY_EXTRA_LIVE_PATTERNS", "prod_key_")
    settings = RelaySettings(_env_file=None)
    assert (settings.enabled, settings.per_minute) == (False, 7)
    assert settings.blocked_user_ids == {USER.id} and settings.extra_live_patterns == ("prod_key_",)


def test_log_filter_masks_keys_and_strips_relay_queries():
    assert mask_secrets("key rzp_test_1DP5mmOl and sk_live_51Pq") == "key rzp_*** and sk_***"
    assert mask_secrets("Authorization: Basic cnpwX3Rlc3RfMQ==") == "Authorization: Basic ***"
    assert mask_secrets("bearer eyJhbGciOiJIUzI1NiIs") == "bearer ***"
    record = logging.LogRecord("httpx", logging.INFO, __file__, 1, 'HTTP Request: %s %s "%s"',
                               ("GET", "https://api.stripe.com/v1/customers?email=a@b.c&key=rk_test_x",
                                "HTTP/1.1 200 OK"), None)
    assert RelaySecretsFilter(("api.stripe.com",)).filter(record) is True
    assert record.getMessage() == 'HTTP Request: GET https://api.stripe.com/v1/customers "HTTP/1.1 200 OK"'
    other = logging.LogRecord("httpx", logging.INFO, __file__, 1, "GET https://api.github.com/x?page=2", None, None)
    RelaySecretsFilter(("api.stripe.com",)).filter(other)
    assert other.getMessage() == "GET https://api.github.com/x?page=2"
    # Pinned relay calls are logged by IP.
    for url in ("https://104.18.12.1/v1/orders?key=abc", "https://[2606:4700::1]/v1/orders?key=abc"):
        pinned = logging.LogRecord("httpx", logging.INFO, __file__, 1, f"POST {url} done", None, None)
        RelaySecretsFilter(("api.stripe.com",)).filter(pinned)
        assert "key=abc" not in pinned.getMessage() and "/v1/orders done" in pinned.getMessage()


def test_log_filter_is_installed_on_the_relay_and_httpx_loggers():
    for name in ("webnest.codelab_relay", "httpx"):
        assert any(isinstance(f, RelaySecretsFilter) for f in logging.getLogger(name).filters)


def test_prefix_methods_and_host_grammar_come_from_one_place():
    from codelab_relay import constants, cors
    from codelab_relay.router import router

    assert router.prefix == constants.RELAY_PATH and cors.is_relay_path(constants.RELAY_PATH + "/x")
    assert relay_dependencies.relay_cors_options()["allow_methods"] == [*constants.RELAY_METHODS, "OPTIONS"]
    for source in relay_sources():
        text = source.read_text(encoding="utf-8")
        if source.name != "constants.py":
            assert "[a-z0-9]([a-z0-9-]*[a-z0-9])?" not in text and '"/api/codelab/relay"' not in text, source


# ---- code rules (section 1) -----------------------------------------------------------


def relay_sources():
    return sorted(PACKAGE_DIR.rglob("*.py"))


def test_no_staticmethod_or_classmethod_in_the_feature():
    for source in relay_sources():
        text = source.read_text(encoding="utf-8")
        assert "staticmethod" not in text and "classmethod" not in text, source


def _is_protocol(node: ast.ClassDef) -> bool:
    return any(getattr(base, "id", getattr(base, "attr", None)) == "Protocol" for base in node.bases)


def _is_plain_init(node) -> bool:
    """__init__ that only stores its dependencies (the shape section 3 uses)."""
    if node.name != "__init__":
        return False
    for statement in node.body:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
            func = statement.value.func
            if isinstance(func, ast.Attribute) and func.attr == "__init__":
                continue
        return False
    return True


def test_every_function_body_is_wrapped_in_try():
    missing = []
    for source in relay_sources():
        tree = ast.parse(source.read_text(encoding="utf-8"))
        protocol_methods = {
            id(item) for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and _is_protocol(node)
            for item in node.body
        }
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or id(node) in protocol_methods:
                continue
            if _is_plain_init(node):
                continue
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                body = body[1:]   # docstring
            # Leading plain assignments (e.g. a timer) may sit outside the try.
            while body and isinstance(body[0], (ast.Assign, ast.AnnAssign)) and len(body) > 1:
                body = body[1:]
            if len(body) != 1 or not isinstance(body[0], ast.Try):
                missing.append(f"{source.name}:{node.lineno} {node.name}")
    assert missing == []


def test_libraries_are_imported_only_by_their_infra_class():
    owners = {"httpx": "httpx_upstream_client.py", "socket": "dns_host_resolver.py"}
    for source in relay_sources():
        tree = ast.parse(source.read_text(encoding="utf-8"))
        imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                    for alias in node.names}
        imported |= {node.module.split(".")[0] for node in ast.walk(tree)
                     if isinstance(node, ast.ImportFrom) and node.module}
        for library, owner in owners.items():
            if library in imported:
                assert source.name == owner, (library, source)
        if "sqlalchemy" in imported or "codelab_relay.models" in source.read_text(encoding="utf-8"):
            assert source.name in ("sql_daily_counter.py", "models.py", "dependencies.py"), source
