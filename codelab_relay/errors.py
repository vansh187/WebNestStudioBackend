"""Domain errors, one class per row of the contract's error table (section 2.0).
The router's exception handler renders them as {"detail", "code", **extra}
with X-WebNest-Relay-Error, so the frontend can tell a relay refusal apart
from the provider's own answer."""


class RelayError(Exception):
    def __init__(self, message: str, code: str, http_status: int, extra: dict | None = None,
                 headers: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.http_status = http_status
        self.extra = extra or {}
        self.headers = headers or {}


def _seconds_text(seconds: float) -> str:
    try:
        return f"{seconds:g}"
    except (TypeError, ValueError):
        return str(seconds)


class BadTarget(RelayError):
    def __init__(self) -> None:
        super().__init__("The relay URL is not valid.", "relay_bad_target", 400)


class LoginRequired(RelayError):
    def __init__(self) -> None:
        super().__init__("Log in to WebNest to call external APIs from the playground.", "relay_login_required", 401)


class HostNotAllowed(RelayError):
    def __init__(self, host: str) -> None:
        super().__init__(f"{host} is not available through the CodeLab relay. Use the provider's sandbox host.",
                         "relay_host_not_allowed", 403, {"host": host})


class ProviderDisabled(RelayError):
    def __init__(self, provider: str) -> None:
        super().__init__("This provider is not available through the CodeLab relay right now.",
                         "relay_provider_disabled", 403, {"provider": provider})


class PathNotAllowed(RelayError):
    def __init__(self, provider: str) -> None:
        super().__init__("This API path is not available through the CodeLab relay.",
                         "relay_path_not_allowed", 403, {"provider": provider})


class LiveKeyBlocked(RelayError):
    def __init__(self, provider: str, hint: str) -> None:
        super().__init__("Live keys are blocked in CodeLab. Real payments are never made from the playground.",
                         "relay_live_key_blocked", 403, {"provider": provider, "hint": hint})


class TestKeyRequired(RelayError):
    __test__ = False   # not a pytest test class

    def __init__(self, provider: str, hint: str) -> None:
        super().__init__("A test-mode key is required to call this provider from CodeLab.",
                         "relay_test_key_required", 403, {"provider": provider, "hint": hint})


class UserBlocked(RelayError):
    def __init__(self) -> None:
        super().__init__("Your account can't use the CodeLab API relay. Contact support if this is a mistake.",
                         "relay_user_blocked", 403)


class MethodNotAllowed(RelayError):
    def __init__(self) -> None:
        super().__init__("The relay accepts GET, POST, PUT, PATCH and DELETE only.", "relay_method_not_allowed", 405)


class PayloadTooLarge(RelayError):
    def __init__(self, limit_bytes: int) -> None:
        super().__init__(f"The request body is larger than the relay limit of {limit_bytes} bytes.",
                         "relay_payload_too_large", 413, {"limit_bytes": limit_bytes})


class RateLimited(RelayError):
    def __init__(self, retry_after: int, scope: str, limit_text: str) -> None:
        super().__init__(f"Relay limit reached: {limit_text}. Try again in {retry_after} seconds.",
                         "relay_rate_limited", 429, {"retry_after": retry_after, "scope": scope},
                         {"Retry-After": str(retry_after)})


class UpstreamUnavailable(RelayError):
    def __init__(self, provider: str, name: str | None = None) -> None:
        super().__init__(f"{name or 'The provider'} could not be reached. Try again in a moment.",
                         "relay_upstream_unavailable", 502, {"provider": provider})


class RedirectBlocked(RelayError):
    def __init__(self, provider: str, status: int, name: str | None = None) -> None:
        super().__init__(f"{name or 'The provider'} answered with a redirect, which the relay never follows.",
                         "relay_redirect_blocked", 502, {"provider": provider, "status": status})


class ResponseTooLarge(RelayError):
    def __init__(self, limit_bytes: int) -> None:
        super().__init__(f"The provider's response is larger than the relay limit of {limit_bytes} bytes.",
                         "relay_response_too_large", 502, {"limit_bytes": limit_bytes})


class RelayDisabled(RelayError):
    def __init__(self, temporary: bool = False) -> None:
        message = ("The CodeLab API relay is unavailable right now. Try again in a moment." if temporary
                   else "The CodeLab API relay is switched off right now.")
        super().__init__(message, "relay_disabled", 503)


class UpstreamTimeout(RelayError):
    def __init__(self, provider: str, timeout_seconds: float, name: str | None = None) -> None:
        super().__init__(f"{name or 'The provider'} did not answer within {_seconds_text(timeout_seconds)} seconds.",
                         "relay_upstream_timeout", 504, {"provider": provider, "timeout_seconds": timeout_seconds})


class RegistryInvalid(RelayError):
    """providers.json failed validation. Raised at import, so the app refuses
    to start instead of relaying with a broken registry."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"Invalid CodeLab relay provider registry: {reason}", "relay_disabled", 503)
