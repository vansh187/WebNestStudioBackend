# Webnest CodeLab API Relay - Backend Team Handoff

This document is for the backend team. It adds an **API relay** so learner code running in the CodeLab backend playground (FastAPI inside the browser, on Pyodide) can call third-party payment APIs in **test mode**: Razorpay, Stripe, PayPal sandbox, and more added by configuration.

`CODELAB_API_RELAY_FRONTEND.md` contains the same Section 2 word for word. Do not change a URL, header, field or error code without updating both documents.

## 0. Why this exists

The learner's Python runs in their browser. A browser may only call another site if that site answers with CORS headers. Payment APIs don't send them, on purpose, so this fails in the playground today:

```python
client = razorpay.Client(auth=(os.environ["RAZORPAY_KEY_ID"], os.environ["RAZORPAY_KEY_SECRET"]))
client.order.create({"amount": 50000, "currency": "INR"})
# requests.exceptions.ConnectionError: Failed to execute 'send' on 'XMLHttpRequest'
```

The relay is an endpoint on our server that makes the call for the browser. CORS doesn't apply to server-to-server calls.

```
learner's Python (browser) ──> POST {API}/api/codelab/relay/api.razorpay.com/v1/orders
                                  our server: logged in? host allowed? test key? under limits?
                                  └──> POST https://api.razorpay.com/v1/orders
                               <── Razorpay's status + body, unchanged
```

The frontend rewrites the URL inside the browser runtime, so learners write normal SDK code and see the provider's real test-mode responses and errors.

**Payment safety is the whole point of the design.** The relay must never move real money and must never be usable as an open proxy. Every rule in Section 6 is mandatory.

## 1. Decisions and mandatory rules

Architecture:

- New module `app/codelab_relay/`, mounted under `/api/codelab/relay`.
- Providers are **data, not code**: a JSON registry (Section 4). Adding a gateway means adding a JSON entry plus a test, never a new code path.
- Every relayed call requires the existing WebNest login, sent in **`X-WebNest-Authorization`**. The standard `Authorization` header belongs to the payment provider (the learner's test key) and is forwarded upstream.

Code rules (mandatory for this feature, same as the GitHub integration):

1. **No `@staticmethod`** anywhere in the feature, and no `@classmethod` used as a static helper. Use instance methods on classes that receive their dependencies through `__init__`, or plain module-level functions.
2. **Every function body in routers, services, rules, stores and clients is wrapped in `try/except`.**
   - Catch specific exceptions first.
   - Log with context (user id, provider id, method, path **without** query string).
   - Re-raise as a domain error from `app/codelab_relay/errors.py`.
   - Never swallow an error silently, and never return a stack trace or an upstream exception message to the client.
3. **Loose coupling:**
   - Routers call services only.
   - Services depend on **`typing.Protocol` interfaces** (`ProviderRegistry`, `UpstreamClient`, `RateLimiter`, `HostResolver`, `TestModeRule`, `Clock`), never on concrete classes.
   - Concrete classes are constructed in exactly one place, `app/codelab_relay/dependencies.py`, and injected with FastAPI `Depends`.
   - Only the class implementing a protocol imports its library: `httpx` only in the upstream client, the ORM only in the stores, `socket` only in the resolver.
4. Match the existing project:
   - the same JWT verification `get_current_user` uses, reading the token from `X-WebNest-Authorization`
   - the same `{ "detail" }` / `422 { "errors" }` shapes
   - ISO-8601 UTC timestamps

## 2. API contract

Base URL: `VITE_API_BASE_URL` (production `https://webneststudiobackend-n00h.onrender.com`). Relay base path: `/api/codelab/relay`.

### 2.0 Headers and error format

Request headers on a relayed call:

| Header | Required | Meaning |
|---|---|---|
| `X-WebNest-Authorization: Bearer <WebNest access token>` | yes | Who the learner is. **Never forwarded upstream.** |
| `Authorization` | provider-specific | The learner's **test** key for the provider. Forwarded upstream unchanged. |
| `Content-Type`, `Accept` and provider headers from the registry (e.g. `Idempotency-Key`, `Stripe-Version`, `PayPal-Request-Id`) | optional | Forwarded only if listed for that provider. |

Every **relay-generated** error carries the response header `X-WebNest-Relay-Error: <code>` and this body:

```json
{ "detail": "Human readable message.", "code": "<machine_code>" }
```

plus the extra fields listed below. An **upstream** response (including the provider's own 400/401/404) is passed through unchanged and **never** has `X-WebNest-Relay-Error`. That header is how the frontend tells "the relay refused" apart from "Razorpay said no".

| HTTP | code | When | Extra fields |
|---|---|---|---|
| 400 | `relay_bad_target` | Host or path malformed: port, user info, `..`, `//`, backslash, encoded `/` or `.` tricks, non-ASCII host | |
| 401 | `relay_login_required` | `X-WebNest-Authorization` missing, invalid or expired | |
| 403 | `relay_host_not_allowed` | Host is not in the registry (includes every **live** host such as `api-m.paypal.com`) | `host` |
| 403 | `relay_provider_disabled` | Provider exists but `enabled` is false | `provider` |
| 403 | `relay_path_not_allowed` | Path doesn't start with one of the provider's `path_prefixes` | `provider` |
| 403 | `relay_live_key_blocked` | A live credential pattern was found in a header, the query string or the body | `provider`, `hint` |
| 403 | `relay_test_key_required` | A `key_prefix` provider got no recognisable test credential | `provider`, `hint` |
| 403 | `relay_user_blocked` | The account is blocked from the relay by an admin | |
| 405 | `relay_method_not_allowed` | Method is not GET, POST, PUT, PATCH or DELETE | |
| 413 | `relay_payload_too_large` | Request body over the limit | `limit_bytes` |
| 429 | `relay_rate_limited` | Per-user minute or day quota used up, or the global day quota | `retry_after` (seconds), `scope` (`minute`, `day` or `global`) |
| 502 | `relay_upstream_unavailable` | DNS failure, connection error, TLS error or an upstream address that isn't public | `provider` |
| 502 | `relay_redirect_blocked` | Upstream answered 3xx; redirects are never followed | `provider`, `status` |
| 502 | `relay_response_too_large` | Upstream body over the limit | `limit_bytes` |
| 503 | `relay_disabled` | Relay switched off (`RELAY_ENABLED=false`) | |
| 504 | `relay_upstream_timeout` | Upstream didn't answer within the timeout | `provider`, `timeout_seconds` |

Headers on **every** relay response (success or error):

- `Cache-Control: no-store`
- `X-Content-Type-Options: nosniff`

On success, also:

- `X-WebNest-Relay-Provider: <provider id>`
- `X-WebNest-Relay-Remaining: <calls left this minute>`

`429` also sends `Retry-After`.

### 2.1 `GET /api/codelab/relay/providers`

Public (no login), `Cache-Control: public, max-age=300`. Lists **enabled** providers only, so the frontend never hard-codes hosts. Request:

```http
GET /api/codelab/relay/providers HTTP/1.1
Host: webneststudiobackend-n00h.onrender.com
```

200:

```json
{
  "enabled": true,
  "relay_path": "/api/codelab/relay",
  "limits": {
    "per_minute": 30,
    "per_day": 300,
    "max_request_bytes": 1048576,
    "max_response_bytes": 2097152,
    "timeout_seconds": 20
  },
  "providers": [
    {
      "id": "razorpay",
      "name": "Razorpay",
      "hosts": ["api.razorpay.com"],
      "test_mode": "key_prefix",
      "test_key_hint": "Use a test key that starts with rzp_test_",
      "docs_url": "https://razorpay.com/docs/api/"
    },
    {
      "id": "stripe",
      "name": "Stripe",
      "hosts": ["api.stripe.com"],
      "test_mode": "key_prefix",
      "test_key_hint": "Use a secret key that starts with sk_test_",
      "docs_url": "https://docs.stripe.com/api"
    },
    {
      "id": "paypal",
      "name": "PayPal (sandbox)",
      "hosts": ["api-m.sandbox.paypal.com"],
      "test_mode": "sandbox_host",
      "test_key_hint": "Use your PayPal sandbox app's client id and secret",
      "docs_url": "https://developer.paypal.com/api/rest/"
    }
  ]
}
```

When the relay is switched off, the response is 200 with `"enabled": false` and `"providers": []`, and the frontend relays nothing.

### 2.2 `GET /api/codelab/relay/usage`

Requires `X-WebNest-Authorization`. Request:

```http
GET /api/codelab/relay/usage HTTP/1.1
X-WebNest-Authorization: Bearer eyJhbGciOiJIUzI1NiIs...
```

200:

```json
{ "minute_remaining": 27, "day_remaining": 288, "day_resets_at": "2026-10-25T00:00:00Z" }
```

### 2.3 `GET|POST|PUT|PATCH|DELETE /api/codelab/relay/{host}/{path}`

- `{host}`: the provider host, lowercase, exactly as listed in 2.1.
- `{path}`: the provider path without the leading `/`.

The query string is forwarded unchanged. The upstream URL is always `https://{host}/{path}?{query}`.

**Sample A: Razorpay, create an order (success)**

Request from the playground:

```http
POST /api/codelab/relay/api.razorpay.com/v1/orders HTTP/1.1
Host: webneststudiobackend-n00h.onrender.com
X-WebNest-Authorization: Bearer eyJhbGciOiJIUzI1NiIs...
Authorization: Basic cnpwX3Rlc3RfMURQNW1tT2xGNUc1YWc6dGhpc2lzYXRlc3RzZWNyZXQ=
Content-Type: application/json

{"amount": 50000, "currency": "INR", "receipt": "rcpt_1001", "notes": {"course": "fastapi"}}
```

(`Authorization` is Basic `rzp_test_1DP5mmOlF5G5ag:thisisatestsecret`.)

The relay sends `POST https://api.razorpay.com/v1/orders` with the same `Authorization`, `Content-Type` and body, and `User-Agent: WebNest-CodeLab-Relay/1.0`. Response 200 (Razorpay's body, unchanged):

```http
HTTP/1.1 200 OK
Content-Type: application/json
X-WebNest-Relay-Provider: razorpay
X-WebNest-Relay-Remaining: 29
Cache-Control: no-store
X-Content-Type-Options: nosniff

{"id": "order_RXq3sYf0nV2kTz", "entity": "order", "amount": 50000, "amount_paid": 0, "amount_due": 50000,
 "currency": "INR", "receipt": "rcpt_1001", "status": "created", "attempts": 0,
 "notes": {"course": "fastapi"}, "created_at": 1761300000}
```

**Sample B: Razorpay rejects the test secret (upstream error, passed through)**

```http
HTTP/1.1 401 Unauthorized
Content-Type: application/json
X-WebNest-Relay-Provider: razorpay
X-WebNest-Relay-Remaining: 28

{"error": {"code": "BAD_REQUEST_ERROR", "description": "Authentication failed", "source": "NA", "step": "NA", "reason": "NA", "metadata": {}}}
```

There is no `X-WebNest-Relay-Error`, so this is Razorpay's answer. The SDK raises its normal `razorpay.errors.BadRequestError`.

**Sample C: live key (relay refuses)**

Request `Authorization: Basic cnpwX2xpdmVfMURQNW1tT2xGNUc1YWc6dGhpc2lzYWxpdmVzZWNyZXQ=` (`rzp_live_...`):

```http
HTTP/1.1 403 Forbidden
X-WebNest-Relay-Error: relay_live_key_blocked
Content-Type: application/json

{"detail": "Live keys are blocked in CodeLab. Real payments are never made from the playground.",
 "code": "relay_live_key_blocked", "provider": "razorpay", "hint": "Use a test key that starts with rzp_test_"}
```

**Sample D: Stripe, create a PaymentIntent (form body)**

```http
POST /api/codelab/relay/api.stripe.com/v1/payment_intents HTTP/1.1
X-WebNest-Authorization: Bearer eyJhbGciOiJIUzI1NiIs...
Authorization: Bearer sk_test_51PqExampleKey
Content-Type: application/x-www-form-urlencoded
Idempotency-Key: 6f1c2a9e-1b7d-4c55-9a8e-2f4b0d3c1e77

amount=50000&currency=inr&automatic_payment_methods[enabled]=true
```

200:

```json
{"id": "pi_3PqExample", "object": "payment_intent", "amount": 50000, "currency": "inr", "status": "requires_payment_method", "livemode": false, "client_secret": "pi_3PqExample_secret_abc"}
```

**Sample E: PayPal sandbox, get an access token**

```http
POST /api/codelab/relay/api-m.sandbox.paypal.com/v1/oauth2/token HTTP/1.1
X-WebNest-Authorization: Bearer eyJhbGciOiJIUzI1NiIs...
Authorization: Basic QWVBMVFJWlhpZmxyMV8tcjBVMlViV1R6aU9XWDFHUlFlci1oSnVTX2NOb0duRFBNVnZGb0c2Vm9sNExiOkVCMmhoUDd5WnlWYTN4QTFEMnZ0eXFvOWJsaEo4VWJFNUp0WWx5bURvQThIMXhWNnRUQlkxT01kOGpGQVA4dFE=
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials
```

200 (PayPal's body). The same call to the **live** host `api-m.paypal.com`:

```http
HTTP/1.1 403 Forbidden
X-WebNest-Relay-Error: relay_host_not_allowed

{"detail": "api-m.paypal.com is not available through the CodeLab relay. Use the provider's sandbox host.",
 "code": "relay_host_not_allowed", "host": "api-m.paypal.com"}
```

**Sample F: not logged in**

```http
HTTP/1.1 401 Unauthorized
X-WebNest-Relay-Error: relay_login_required

{"detail": "Log in to WebNest to call external APIs from the playground.", "code": "relay_login_required"}
```

**Sample G: rate limited**

```http
HTTP/1.1 429 Too Many Requests
X-WebNest-Relay-Error: relay_rate_limited
Retry-After: 41

{"detail": "Relay limit reached: 30 calls a minute. Try again in 41 seconds.", "code": "relay_rate_limited", "retry_after": 41, "scope": "minute"}
```

**Sample H: upstream timeout**

```http
HTTP/1.1 504 Gateway Timeout
X-WebNest-Relay-Error: relay_upstream_timeout

{"detail": "Razorpay did not answer within 20 seconds.", "code": "relay_upstream_timeout", "provider": "razorpay", "timeout_seconds": 20}
```

**Sample I: bad target**

`GET /api/codelab/relay/api.razorpay.com/v1/../../admin`:

```http
HTTP/1.1 400 Bad Request
X-WebNest-Relay-Error: relay_bad_target

{"detail": "The relay URL is not valid.", "code": "relay_bad_target"}
```

### 2.4 CORS for relay routes

- Allowed origins: the existing frontend origins only (`https://www.webneststudio.co.in`, `http://127.0.0.1:5173`, `http://localhost:4173`). Never `*`.
- `Access-Control-Allow-Credentials`: not set (no cookies are used).
- Allowed methods: `GET, POST, PUT, PATCH, DELETE, OPTIONS`.
- Allowed headers: `X-WebNest-Authorization` plus the union of every enabled provider's `forward_request_headers`, listed explicitly. `Authorization` is never covered by `*`.
- Exposed headers: `X-WebNest-Relay-Error, X-WebNest-Relay-Provider, X-WebNest-Relay-Remaining, Retry-After` plus provider response headers such as `Request-Id`.
- `Access-Control-Max-Age: 600`.

## 3. Module layout

```
app/codelab_relay/
  router.py                 # HTTP only: parse, call service, map domain errors to HTTP
  schemas.py                # Pydantic models for 2.1 and 2.2
  errors.py                 # RelayError base + one subclass per contract code
  protocols.py              # ProviderRegistry, UpstreamClient, RateLimiter, HostResolver, TestModeRule, Clock
  settings.py               # RelaySettings, read from env (Section 7)
  providers.json            # the registry (Section 4)
  services/
    target_parser.py        # TargetParser: host/path validation and upstream URL
    header_filter.py        # HeaderFilter: allowlists both ways
    test_mode.py            # KeyPrefixRule, SandboxHostRule, LivePatternScanner, TestModeRuleFactory
    relay_service.py        # RelayService: orchestrates one relayed call
    usage_service.py        # UsageService: 2.2
  infra/
    json_provider_registry.py
    httpx_upstream_client.py   # the only file importing httpx
    memory_minute_limiter.py
    sql_daily_counter.py       # the only file using the ORM
    dns_host_resolver.py       # the only file importing socket
    system_clock.py
  dependencies.py           # the ONLY place concrete classes are constructed
  models.py                 # ORM tables (Section 5)
```

### 3.1 Errors (`errors.py`)

```python
class RelayError(Exception):
    def __init__(self, message: str, code: str, http_status: int, extra: dict | None = None,
                 headers: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.http_status = http_status
        self.extra = extra or {}
        self.headers = headers or {}


class HostNotAllowed(RelayError):
    def __init__(self, host: str) -> None:
        super().__init__(f"{host} is not available through the CodeLab relay. Use the provider's sandbox host.",
                         "relay_host_not_allowed", 403, {"host": host})


class LiveKeyBlocked(RelayError):
    def __init__(self, provider: str, hint: str) -> None:
        super().__init__("Live keys are blocked in CodeLab. Real payments are never made from the playground.",
                         "relay_live_key_blocked", 403, {"provider": provider, "hint": hint})


class RateLimited(RelayError):
    def __init__(self, retry_after: int, scope: str, limit_text: str) -> None:
        super().__init__(f"Relay limit reached: {limit_text}. Try again in {retry_after} seconds.",
                         "relay_rate_limited", 429, {"retry_after": retry_after, "scope": scope},
                         {"Retry-After": str(retry_after)})

# Also one class per remaining row of the 2.0 table: BadTarget, LoginRequired, ProviderDisabled,
# PathNotAllowed, TestKeyRequired, UserBlocked, MethodNotAllowed, PayloadTooLarge,
# UpstreamUnavailable, RedirectBlocked, ResponseTooLarge, RelayDisabled, UpstreamTimeout.
```

### 3.2 Protocols (`protocols.py`)

```python
@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    enabled: bool
    hosts: tuple[str, ...]
    path_prefixes: tuple[str, ...]
    test_mode: dict            # {"rule": "key_prefix" | "sandbox_host", ...} (Section 4)
    forward_request_headers: tuple[str, ...]
    forward_response_headers: tuple[str, ...]
    test_key_hint: str
    docs_url: str


@dataclass(frozen=True)
class OutgoingRequest:
    method: str
    url: str                   # always https://<host>/<path>?<query>
    headers: dict[str, str]
    body: bytes


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
    async def consume(self, user_id: int, now: datetime) -> int: ...   # returns calls left this minute, or raises RateLimited
    async def remaining(self, user_id: int, now: datetime) -> dict: ...


class HostResolver(Protocol):
    async def assert_public(self, host: str) -> None: ...   # raises UpstreamUnavailable for private/loopback/reserved IPs


class TestModeRule(Protocol):
    def check(self, provider: Provider, headers: dict[str, str], query: str, body: bytes) -> None: ...


class Clock(Protocol):
    def now(self) -> datetime: ...
```

### 3.3 Target parsing (`services/target_parser.py`)

```python
_HOST = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
_BAD_PATH = re.compile(r"(^|/)\.\.?(/|$)|//|\\|%2e|%2f|%5c|%00", re.IGNORECASE)


class TargetParser:
    def __init__(self, registry: ProviderRegistry) -> None:
        self._registry = registry

    def parse(self, host: str, path: str, query: str) -> tuple[Provider, str]:
        try:
            if not _HOST.fullmatch(host) or len(host) > 253:
                raise BadTarget()
            if _BAD_PATH.search(path) or any(ord(char) < 0x21 for char in path):
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
            if urlsplit(url).hostname != host or urlsplit(url).port is not None:
                raise BadTarget()
            return provider, url
        except RelayError:
            raise
        except Exception as exc:
            logger.exception("target parse failed host=%s", host)
            raise BadTarget() from exc
```

### 3.4 Test-mode rules (`services/test_mode.py`)

```python
# Live credential patterns blocked for every provider, wherever they appear.
GLOBAL_LIVE_PATTERNS = ("rzp_live_", "sk_live_", "rk_live_", "pk_live_")


class LivePatternScanner:
    def __init__(self, patterns: tuple[str, ...]) -> None:
        self._patterns = tuple(pattern.lower() for pattern in patterns)

    def find(self, headers: dict[str, str], query: str, body: bytes) -> bool:
        try:
            texts = [value.lower() for value in headers.values()]
            texts += [self._decoded_basic(value) for value in headers.values()]
            texts += [unquote(query).lower(), body[:2_000_000].decode("utf-8", "ignore").lower()]
            return any(pattern in text for text in texts for pattern in self._patterns)
        except Exception as exc:
            logger.exception("live pattern scan failed")
            raise LiveKeyBlocked("unknown", "Use a test key.") from exc   # fail closed

    def _decoded_basic(self, value: str) -> str:
        try:
            if not value.lower().startswith("basic "):
                return ""
            return base64.b64decode(value[6:].strip(), validate=True).decode("utf-8", "ignore").lower()
        except (binascii.Error, ValueError):
            return ""


class KeyPrefixRule:
    """Razorpay, Stripe: the credential itself says test or live."""

    def __init__(self, scanner: LivePatternScanner) -> None:
        self._scanner = scanner

    def check(self, provider: Provider, headers: dict[str, str], query: str, body: bytes) -> None:
        try:
            if self._scanner.find(headers, query, body):
                raise LiveKeyBlocked(provider.id, provider.test_key_hint)
            credential = self._credential(headers.get("authorization", ""))
            allowed = provider.test_mode.get("allowed_prefixes", [])
            if not credential or not any(credential.startswith(prefix) for prefix in allowed):
                raise TestKeyRequired(provider.id, provider.test_key_hint)
        except RelayError:
            raise
        except Exception as exc:
            logger.exception("key prefix check failed provider=%s", provider.id)
            raise TestKeyRequired(provider.id, provider.test_key_hint) from exc   # fail closed

    def _credential(self, authorization: str) -> str:
        try:
            scheme, _, value = authorization.partition(" ")
            if scheme.lower() == "bearer":
                return value.strip()
            if scheme.lower() == "basic":
                decoded = base64.b64decode(value.strip(), validate=True).decode("utf-8")
                return decoded.split(":", 1)[0]   # key id / username
            return ""
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return ""


class SandboxHostRule:
    """PayPal, Cashfree, PayU...: only the sandbox host is in the registry, so the host
    itself guarantees test mode. Still blocks live patterns as defence in depth."""

    def __init__(self, scanner: LivePatternScanner) -> None:
        self._scanner = scanner

    def check(self, provider: Provider, headers: dict[str, str], query: str, body: bytes) -> None:
        try:
            if self._scanner.find(headers, query, body):
                raise LiveKeyBlocked(provider.id, provider.test_key_hint)
        except RelayError:
            raise
        except Exception as exc:
            logger.exception("sandbox host check failed provider=%s", provider.id)
            raise LiveKeyBlocked(provider.id, provider.test_key_hint) from exc


class TestModeRuleFactory:
    def __init__(self, key_prefix: TestModeRule, sandbox_host: TestModeRule) -> None:
        self._rules = {"key_prefix": key_prefix, "sandbox_host": sandbox_host}

    def for_provider(self, provider: Provider) -> TestModeRule:
        try:
            return self._rules[provider.test_mode["rule"]]
        except KeyError as exc:
            logger.error("unknown test mode rule provider=%s", provider.id)
            raise ProviderDisabled(provider.id) from exc   # an unknown rule never relays
```

### 3.5 Service (`services/relay_service.py`)

```python
class RelayService:
    def __init__(self, settings: RelaySettings, parser: TargetParser, headers: HeaderFilter,
                 rules: TestModeRuleFactory, limiter: RateLimiter, resolver: HostResolver,
                 upstream: UpstreamClient, clock: Clock) -> None:
        self._settings = settings
        self._parser = parser
        self._headers = headers
        self._rules = rules
        self._limiter = limiter
        self._resolver = resolver
        self._upstream = upstream
        self._clock = clock

    async def relay(self, user_id: int, method: str, host: str, path: str, query: str,
                    incoming_headers: dict[str, str], body: bytes) -> tuple[UpstreamResponse, Provider, int]:
        try:
            if not self._settings.enabled:
                raise RelayDisabled()
            if user_id in self._settings.blocked_user_ids:
                raise UserBlocked()
            if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                raise MethodNotAllowed()
            if len(body) > self._settings.max_request_bytes:
                raise PayloadTooLarge(self._settings.max_request_bytes)
            provider, url = self._parser.parse(host, path, query)
            outgoing_headers = self._headers.outgoing(provider, incoming_headers)
            self._rules.for_provider(provider).check(provider, outgoing_headers, query, body)
            remaining = await self._limiter.consume(user_id, self._clock.now())
            await self._resolver.assert_public(host)
            response = await self._upstream.send(
                OutgoingRequest(method, url, outgoing_headers, body),
                self._settings.max_response_bytes, self._settings.timeout_seconds)
            if 300 <= response.status < 400:
                raise RedirectBlocked(provider.id, response.status)
            logger.info("relay user=%s provider=%s method=%s path=%s status=%s bytes=%s",
                        user_id, provider.id, method, "/" + path, response.status, len(response.body))
            return response, provider, remaining
        except RelayError:
            raise
        except Exception as exc:
            logger.exception("relay failed user=%s host=%s method=%s", user_id, host, method)
            raise UpstreamUnavailable("unknown") from exc
```

Order matters: validate the target, filter headers, check test mode and rate limits **before** any DNS lookup or network call. A refused request costs nothing upstream.

### 3.6 Header filter (`services/header_filter.py`)

- **Outgoing:** keep only the provider's `forward_request_headers` (lowercased). Then:
  - always drop `x-webnest-authorization`, `cookie`, `host`, `origin`, `referer`, `x-forwarded-*`, `forwarded`, `x-real-ip`, `via`, `connection`, `keep-alive`, `te`, `trailer`, `transfer-encoding`, `upgrade`, `proxy-*`, `content-length`
  - set `User-Agent: WebNest-CodeLab-Relay/1.0`
- **Incoming (to the browser):** keep only `content-type` plus the provider's `forward_response_headers`. Always drop `set-cookie`, `location`, `content-encoding`, `content-length`, `transfer-encoding`, `strict-transport-security`, `alt-svc`. Then add the headers from 2.0.

Every method has its own `try/except` that logs and raises `BadTarget` (outgoing) or `UpstreamUnavailable` (incoming).

### 3.7 Upstream client (`infra/httpx_upstream_client.py`)

```python
class HttpxUpstreamClient:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client     # follow_redirects=False, verify=True, trust_env=False

    async def send(self, request: OutgoingRequest, max_response_bytes: int, timeout_seconds: float) -> UpstreamResponse:
        try:
            async with asyncio.timeout(timeout_seconds):
                async with self._client.stream(request.method, request.url, headers=request.headers,
                                               content=request.body or None) as response:
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
            raise UpstreamTimeout(urlsplit(request.url).hostname, timeout_seconds) from exc
        except httpx.HTTPError as exc:
            logger.warning("upstream error host=%s type=%s", urlsplit(request.url).hostname, type(exc).__name__)
            raise UpstreamUnavailable(urlsplit(request.url).hostname) from exc
```

The client only knows the host. `RelayService` catches `UpstreamTimeout` and `UpstreamUnavailable` from it and raises them again with the provider id and name (for example "Razorpay did not answer within 20 seconds.") before they reach the router.

`trust_env=False` stops environment proxy variables from rerouting calls. Never log `request.headers`, `request.body` or the full URL with its query string.

### 3.8 Router (`router.py`)

```python
router = APIRouter(prefix="/api/codelab/relay", tags=["codelab-relay"])


@router.get("/providers", response_model=ProvidersResponse)
async def list_providers(response: Response, service: ProviderListService = Depends(get_provider_list_service)):
    try:
        response.headers["Cache-Control"] = "public, max-age=300"
        return service.describe()
    except RelayError as error:
        raise to_http(error)
    except Exception as exc:
        logger.exception("unexpected error in list_providers")
        raise to_http(UpstreamUnavailable("unknown")) from exc


@router.get("/usage", response_model=UsageResponse)
async def usage(user=Depends(get_relay_user), service: UsageService = Depends(get_usage_service)):
    try:
        return await service.for_user(user.id)
    except RelayError as error:
        raise to_http(error)
    except Exception as exc:
        logger.exception("unexpected error in usage")
        raise to_http(UpstreamUnavailable("unknown")) from exc


@router.api_route("/{host}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def relay(host: str, path: str, request: Request, user=Depends(get_relay_user),
                service: RelayService = Depends(get_relay_service),
                reader: BodyReader = Depends(get_body_reader)):
    try:
        body = await reader.read(request)           # streams with the size cap, raises PayloadTooLarge
        upstream, provider, remaining = await service.relay(
            user.id, request.method, host.lower(), path, request.url.query,
            {name.lower(): value for name, value in request.headers.items()}, body)
        return build_response(upstream, provider, remaining)   # applies 3.6 incoming filter + 2.0 headers
    except RelayError as error:
        raise to_http(error)
    except Exception as exc:
        logger.exception("unexpected error in relay host=%s", host)
        raise to_http(UpstreamUnavailable("unknown")) from exc
```

- `get_relay_user` reads `X-WebNest-Authorization`. It reuses the **same token verification function** `get_current_user` uses and raises `LoginRequired` (401 `relay_login_required`) when the token is missing or invalid. Inactive or unverified accounts are refused the same way.
- `to_http(error)` and the registered exception handler render `{"detail": message, "code": code, **extra}`, add `error.headers`, and **always** add `X-WebNest-Relay-Error: <code>`, `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`.
- Declare the fixed routes `/providers` and `/usage` **before** the catch-all route.

### 3.9 Wiring (`dependencies.py`)

```python
def get_relay_service(db=Depends(get_db)) -> RelayService:
    try:
        settings = get_relay_settings()
        registry = get_provider_registry()             # loaded once at startup, cached
        scanner = LivePatternScanner(GLOBAL_LIVE_PATTERNS + settings.extra_live_patterns)
        rules = TestModeRuleFactory(KeyPrefixRule(scanner), SandboxHostRule(scanner))
        limiter = CompositeLimiter(get_minute_limiter(), SqlDailyCounter(db, settings))
        return RelayService(settings, TargetParser(registry), HeaderFilter(), rules, limiter,
                            DnsHostResolver(), HttpxUpstreamClient(get_shared_httpx_client()), SystemClock())
    except Exception as exc:
        logger.exception("relay wiring failed")
        raise to_http(RelayDisabled()) from exc
```

Tests override these functions with fakes through `app.dependency_overrides`.

## 4. Provider registry (`providers.json`)

Loaded once at startup by `JsonProviderRegistry`, which validates every entry and refuses to start on a bad file:
- a host may appear only once
- hosts are lowercase
- `rule` is known
- `path_prefixes` are non-empty and start with `/`

```json
[
  {
    "id": "razorpay",
    "name": "Razorpay",
    "enabled": true,
    "hosts": ["api.razorpay.com"],
    "path_prefixes": ["/v1/", "/v2/"],
    "test_mode": { "rule": "key_prefix", "allowed_prefixes": ["rzp_test_"] },
    "forward_request_headers": ["authorization", "content-type", "accept"],
    "forward_response_headers": [],
    "test_key_hint": "Use a test key that starts with rzp_test_",
    "docs_url": "https://razorpay.com/docs/api/"
  },
  {
    "id": "stripe",
    "name": "Stripe",
    "enabled": true,
    "hosts": ["api.stripe.com"],
    "path_prefixes": ["/v1/"],
    "test_mode": { "rule": "key_prefix", "allowed_prefixes": ["sk_test_", "rk_test_"] },
    "forward_request_headers": ["authorization", "content-type", "accept", "idempotency-key", "stripe-version"],
    "forward_response_headers": ["request-id"],
    "test_key_hint": "Use a secret key that starts with sk_test_",
    "docs_url": "https://docs.stripe.com/api"
  },
  {
    "id": "paypal",
    "name": "PayPal (sandbox)",
    "enabled": true,
    "hosts": ["api-m.sandbox.paypal.com"],
    "path_prefixes": ["/v1/", "/v2/"],
    "test_mode": { "rule": "sandbox_host" },
    "forward_request_headers": ["authorization", "content-type", "accept", "paypal-request-id", "prefer"],
    "forward_response_headers": ["paypal-debug-id"],
    "test_key_hint": "Use your PayPal sandbox app's client id and secret",
    "docs_url": "https://developer.paypal.com/api/rest/"
  },
  {
    "id": "cashfree",
    "name": "Cashfree (sandbox)",
    "enabled": false,
    "hosts": ["sandbox.cashfree.com"],
    "path_prefixes": ["/pg/"],
    "test_mode": { "rule": "sandbox_host" },
    "forward_request_headers": ["content-type", "accept", "x-client-id", "x-client-secret", "x-api-version", "x-idempotency-key"],
    "forward_response_headers": ["x-request-id"],
    "test_key_hint": "Use your Cashfree sandbox app id and secret",
    "docs_url": "https://www.cashfree.com/docs/api-reference"
  }
]
```

**Adding a gateway** (PayU, PhonePe, Paytm and others):
1. Confirm from the provider's **current** docs how test mode is separated: a sandbox host (preferred: `sandbox_host`) or a credential prefix (`key_prefix`).
2. Add the entry with `"enabled": false`.
3. Add a router test for a test call, a live call and a wrong host.
4. Try the provider's Python SDK end to end in the playground.
5. Only then set `"enabled": true`.

**Never** list a provider whose live and test traffic share one host *and* whose credentials don't reveal test mode. That provider cannot be made safe.

## 5. Data model

- `codelab_relay_daily_usage`: `user_id` (FK users, on delete cascade), `day` (date, UTC), `count` (int), `updated_at`. Primary key `(user_id, day)`.
  - Increment with an atomic upsert (`INSERT ... ON CONFLICT (user_id, day) DO UPDATE SET count = count + 1`), then compare.
  - Delete rows older than 30 days (startup task or cron).
- Minute limits live in memory (`MemoryMinuteLimiter`, a sliding window per user guarded by an `asyncio.Lock`). On Render's single instance that's exact. If the API ever runs more than one instance, replace it with a Redis implementation of the same `RateLimiter` protocol. No other code changes.
- The global day cap (`RELAY_GLOBAL_PER_DAY`) is a row with `user_id = 0` in the same table.

## 6. Security fixes (all mandatory)

| # | Threat | Fix |
|---|---|---|
| 1 | **Open proxy / SSRF**: relay used to reach any host, internal services or cloud metadata (`169.254.169.254`) | Exact host allowlist from the registry; `https` only; no ports, user info or IP literals; path prefixes per provider; the URL is rebuilt and its host re-checked after parsing (3.3); DNS answers must be public IPs (`ipaddress` global check: no private, loopback, link-local, reserved or multicast) |
| 2 | **Real money moving** | Live hosts are never listed; `key_prefix` providers need a test prefix; live patterns (`rzp_live_`, `sk_live_`, `rk_live_`, `pk_live_` and env extras) are blocked in headers, decoded Basic auth, query and body for **every** provider; rules **fail closed** on any internal error |
| 3 | **Redirect to an unlisted host** | `follow_redirects=False`; any 3xx becomes `502 relay_redirect_blocked`; `Location` is never returned |
| 4 | **Anonymous abuse** | WebNest login required in `X-WebNest-Authorization`; inactive or blocked users refused; per-user minute and day limits plus a global day cap; kill switch `RELAY_ENABLED` |
| 5 | **Key leakage in logs** | Never log `Authorization`, `X-WebNest-Authorization`, cookies, bodies or query strings. Log only user id, provider, method, path without query, status, byte count and duration. Add a logging filter that masks `rzp_*`, `sk_*` and `Basic ...` if they ever reach a log line |
| 6 | **WebNest token sent to a provider** | `x-webnest-authorization` is stripped from outgoing headers; covered by a test |
| 7 | **Header smuggling / spoofing** | Outgoing header allowlist per provider; hop-by-hop, `host`, `x-forwarded-*`, `forwarded`, `cookie` always dropped; header values containing CR or LF are rejected as `relay_bad_target` |
| 8 | **Response-side attacks** (cookies set on our domain, content sniffing, cache poisoning) | Response header allowlist; `set-cookie` always dropped; `X-Content-Type-Options: nosniff`; `Cache-Control: no-store` |
| 9 | **Resource exhaustion** | Request body cap (streamed, not trusting `Content-Length`), response body cap (streamed), 5 s connect / 20 s total timeout, shared `httpx.AsyncClient` with connection limits (`max_connections=50`) |
| 10 | **Path traversal / encoding tricks** | `..`, `//`, backslash, `%2e`, `%2f`, `%5c`, `%00` and control characters rejected before lookup |
| 11 | **CORS misuse** | Explicit origins, methods and headers; no credentials; no wildcard |
| 12 | **Error leakage** | Clients get only the contract messages; upstream exception text and stack traces stay in server logs |
| 13 | **Environment proxy hijack** | `trust_env=False` on the httpx client |
| 14 | **Registry mistakes** | Registry validated at startup; new providers ship disabled; a CI test fails if any enabled host contains `live` or matches a known live host list |

## 7. Configuration (environment variables)

| Name | Example | Notes |
|---|---|---|
| `RELAY_ENABLED` | `true` | Kill switch. `false` returns `503 relay_disabled` and an empty provider list |
| `RELAY_PER_MINUTE` | `30` | Per-user sliding minute |
| `RELAY_PER_DAY` | `300` | Per-user UTC day |
| `RELAY_GLOBAL_PER_DAY` | `5000` | All users together |
| `RELAY_MAX_REQUEST_BYTES` | `1048576` | 1 MB |
| `RELAY_MAX_RESPONSE_BYTES` | `2097152` | 2 MB |
| `RELAY_TIMEOUT_SECONDS` | `20` | Total upstream time |
| `RELAY_EXTRA_LIVE_PATTERNS` | `live_secret_,prod_key_` | Comma separated, added to the global list |
| `RELAY_BLOCKED_USER_IDS` | `1042,2210` | Comma separated |
| `RELAY_PROVIDERS_FILE` | `app/codelab_relay/providers.json` | Registry path |

Add `X-WebNest-Authorization` and the provider headers to the CORS configuration as described in 2.4.

## 8. Tests (pytest)

- **Unit tests with fakes (no network):**
  - `TargetParser`: every bad-target pattern, unknown host, disabled provider, path prefix, rebuilt-host check
  - `KeyPrefixRule`: Bearer and Basic test keys pass; live keys in Authorization, another header, query or body are blocked; missing key; malformed base64
  - `SandboxHostRule`: live pattern blocked
  - `HeaderFilter`: `x-webnest-authorization`, `cookie`, `host` never forwarded; `set-cookie` and `location` never returned
  - `RelayService`: order of checks (no upstream call on refusal), redirect blocking, kill switch, blocked user
- **Router tests with `TestClient` and `dependency_overrides`:**
  - every row of the 2.0 table returns the documented status, `code`, extra fields and `X-WebNest-Relay-Error`
  - upstream 401 passes through **without** that header
- **`HttpxUpstreamClient` tests with `httpx.MockTransport`:** success, timeout, connection error, response over the cap.
- **`DnsHostResolver` tests:** a host resolving to `127.0.0.1` or `10.0.0.5` is refused.
- **Registry tests:** the shipped `providers.json` loads, has no duplicate hosts, and no enabled host looks live.
- **Code rule checks in CI:**
  - `grep -rn "staticmethod" app/codelab_relay` returns nothing
  - a test asserts every function in the module has a top-level `try` (AST walk)

## 9. Backend checklist

- [ ] `errors.py`, `protocols.py`, `schemas.py` (Section 2 exactly), `settings.py`
- [ ] `TargetParser`, `HeaderFilter`, test-mode rules, `RelayService`, `UsageService`, `ProviderListService`
- [ ] Infra: httpx client, resolver, minute limiter, SQL daily counter, clock, JSON registry
- [ ] Table + migration (Section 5)
- [ ] Router + exception handler + `dependencies.py`
- [ ] CORS updated (2.4)
- [ ] Env vars on Render (Section 7)
- [ ] All Section 6 fixes in place, all Section 8 tests green
- [ ] No `@staticmethod`; every function body has try/except
- [ ] Tell the frontend team when it's deployed, with the enabled provider list

## 10. Implementation notes (this repository)

The API contract (Section 2) is implemented exactly. Where this codebase differs from the plan:

- **Location:** the module is the top-level package `codelab_relay/` (like `exams/`), not `app/codelab_relay/`. `RELAY_PROVIDERS_FILE` defaults to `codelab_relay/providers.json`.
- **User ids are UUIDs.** `RELAY_BLOCKED_USER_IDS` takes UUIDs; entries that aren't UUIDs are logged and ignored.
- **Global day cap:** a separate table, `codelab_relay_global_daily_usage(day, count)`, instead of a `user_id = 0` row, which the foreign key to `users` would refuse. Both tables are created at startup by `create_all`; `migrations/013_codelab_api_relay.sql` has the same DDL.
- **Quota upserts** only update while the count is under the limit (`ON CONFLICT ... DO UPDATE ... WHERE count < limit`), so a refused call isn't counted and concurrent calls can't overshoot. A day or global refusal also gives back the minute slot.
- **CORS:** `core/path_scoped_cors.py` (shared with the database proxy) sends `/api/codelab/relay/*` through the relay policy of 2.4 and every other route through the existing site-wide policy. Relay origins come from `RELAY_CORS_ORIGINS` (default: the three origins in 2.4; `*` is dropped).
- **Settings are read from real environment variables only.** The `.env` source of pydantic-settings 2.6 ignores `env_prefix`, so `.env`'s `CORS_ORIGINS=*` would otherwise leak into the relay's settings. Set `RELAY_*` on Render, not in `.env`. `RELAY_ENABLED` defaults to `true`.
- **Extra settings:** `RELAY_CONNECT_TIMEOUT_SECONDS` (default 5) and `RELAY_CORS_ORIGINS`.
- **Logging:** `codelab_relay/logging_filter.py` is installed on the `webnest.codelab_relay` and `httpx` loggers. httpx logs every request URL at INFO, so the filter strips the query string from relay-host URLs, and it masks `rzp_*`, `sk_*`, `rk_*`, `pk_*`, `Basic ...` and `Bearer ...` values.
- **Other methods:** `HEAD`, `OPTIONS` (non-preflight) and `TRACE` are routed to the relay so they get `405 relay_method_not_allowed` instead of a bare 405.
- **Login:** `X-WebNest-Authorization` is checked with `AuthService.get_current_user`, the same check `get_current_user` uses. Inactive accounts get `401 relay_login_required`. Unverified accounts are refused only while `EMAIL_ENABLED` is true, the same rule login applies. The login's database session is closed straight after the check, so no connection is held during the upstream call.
- **Early refusals:** a switched-off relay (503) and an unsupported method (405) are answered before the login check and before any body is read.
- **DNS rebinding:** `HostResolver.resolve_public` returns the public addresses it checked. The upstream call connects to the first of them (TLS SNI, the certificate check and `Host` still use the real host name), so the host is never resolved a second time.
- **Smuggled queries:** a decoded `?` or `#` in the path (from `%3F` / `%23`) is `400 relay_bad_target`.
- **Quota refunds:** when the relay itself fails after counting the call (DNS, timeout, unreachable, redirect blocked, response too large), the day's call is given back (user and global). The minute slot stays used, so retries are still throttled.
- **Quota storage:** `SqlDailyCounter` uses its own short session for each operation. When the global cap refuses a call, the transaction is rolled back, which also undoes the user's increment.
- **Minute limiter:** no `asyncio.Lock`. Its methods never await, so on one event loop they can't interleave, and a lock would be bound to the first loop that contends for it.
- **HTTP client:** `HttpxUpstreamClient` builds its `httpx.AsyncClient` lazily, and rebuilds it after `aclose()` or on a new event loop.
- **Shared constants:** the relay path, methods and host grammar live once, in `codelab_relay/constants.py`.
- **Internal failures** (database down, wiring errors) answer `503 relay_disabled` with a "try again in a moment" message.
- **Code-rule tests:** "every function has a top-level `try`" exempts `Protocol` stubs and `__init__` methods that only store their dependencies, matching the shapes in Section 3.
- **Tests:** `tests/test_codelab_relay.py` (no network, no database) and `tests/test_codelab_relay_sql.py` (real Postgres via `EXAMS_TEST_DATABASE_URL`, a throwaway database, never production).
