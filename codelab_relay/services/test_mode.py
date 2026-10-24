"""Test-mode rules (section 3.4, security fix 2). Every rule fails closed: an
internal error refuses the call instead of relaying it."""

import base64
import binascii
import logging
from urllib.parse import unquote, unquote_plus

from codelab_relay.errors import LiveKeyBlocked, ProviderDisabled, RelayError, TestKeyRequired
from codelab_relay.protocols import Provider, TestModeRule

logger = logging.getLogger("webnest.codelab_relay")

# Live credential patterns blocked for every provider, wherever they appear.
GLOBAL_LIVE_PATTERNS = ("rzp_live_", "sk_live_", "rk_live_", "pk_live_")

MAX_SCANNED_BODY_BYTES = 2_000_000


class LivePatternScanner:
    def __init__(self, patterns: tuple[str, ...]) -> None:
        self._patterns = tuple(pattern.lower() for pattern in patterns)

    def find(self, headers: dict[str, str], query: str, body: bytes) -> bool:
        try:
            body_text = body[:MAX_SCANNED_BODY_BYTES].decode("utf-8", "ignore")
            texts = [value.lower() for value in headers.values()]
            texts += [self._decoded_basic(value) for value in headers.values()]
            # Form bodies may percent-encode the underscores (sk%5Flive%5F).
            texts += [unquote(query).lower(), body_text.lower(), unquote_plus(body_text).lower()]
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
    __test__ = False   # not a pytest test class

    def __init__(self, key_prefix: TestModeRule, sandbox_host: TestModeRule) -> None:
        self._rules = {"key_prefix": key_prefix, "sandbox_host": sandbox_host}

    def for_provider(self, provider: Provider) -> TestModeRule:
        try:
            return self._rules[provider.test_mode["rule"]]
        except (KeyError, TypeError) as exc:
            logger.error("unknown test mode rule provider=%s", provider.id)
            raise ProviderDisabled(provider.id) from exc   # an unknown rule never relays
