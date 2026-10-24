"""The provider registry (section 4): providers are data, not code. The file
is validated once at startup and a bad file stops the app from starting
(security fix 14)."""

import json
import logging
import re
from pathlib import Path

from codelab_relay.constants import HOST_PATTERN
from codelab_relay.errors import RegistryInvalid, RelayError
from codelab_relay.protocols import Provider
from codelab_relay.services.header_filter import DROPPED_REQUEST_HEADERS, DROPPED_REQUEST_PREFIXES

logger = logging.getLogger("webnest.codelab_relay")

KNOWN_RULES = ("key_prefix", "sandbox_host")
# Production hosts of providers that separate test mode by host. None of these
# may ever be enabled; key_prefix providers (Razorpay, Stripe) share one host
# for both modes and are made safe by their credentials instead.
KNOWN_LIVE_HOSTS = frozenset({
    "api-m.paypal.com", "api.paypal.com", "www.paypal.com",
    "api.cashfree.com", "secure.payu.in", "info.payu.in", "api.phonepe.com", "securegw.paytm.in",
})

_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_HEADER = re.compile(r"^[a-z0-9-]+$")
_REQUIRED = ("id", "name", "enabled", "hosts", "path_prefixes", "test_mode", "forward_request_headers",
             "forward_response_headers", "test_key_hint", "docs_url")


class JsonProviderRegistry:
    def __init__(self, providers: list[Provider]) -> None:
        self._providers = tuple(providers)
        self._by_host = {host: provider for provider in providers for host in provider.hosts}

    def all_enabled(self) -> list[Provider]:
        try:
            return [provider for provider in self._providers if provider.enabled]
        except Exception as exc:
            logger.exception("listing enabled providers failed")
            raise RegistryInvalid("could not list providers") from exc

    def for_host(self, host: str) -> Provider | None:
        try:
            return self._by_host.get(host)
        except TypeError as exc:
            raise RegistryInvalid("host lookup failed") from exc


def _string_list(entry: dict, key: str, provider_id: str, allow_empty: bool) -> tuple[str, ...]:
    try:
        values = entry[key]
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise RegistryInvalid(f"{provider_id}: {key} must be a list of strings")
        if not values and not allow_empty:
            raise RegistryInvalid(f"{provider_id}: {key} must not be empty")
        return tuple(values)
    except RelayError:
        raise
    except Exception as exc:
        raise RegistryInvalid(f"{provider_id}: {key} could not be read") from exc


def _parse_entry(entry: object) -> Provider:
    try:
        if not isinstance(entry, dict):
            raise RegistryInvalid("every provider must be an object")
        missing = [key for key in _REQUIRED if key not in entry]
        if missing:
            raise RegistryInvalid(f"provider is missing {', '.join(missing)}")
        provider_id = entry["id"]
        if not isinstance(provider_id, str) or not _ID.fullmatch(provider_id):
            raise RegistryInvalid(f"invalid provider id {provider_id!r}")
        for key in ("name", "test_key_hint", "docs_url"):
            if not isinstance(entry[key], str) or not entry[key].strip():
                raise RegistryInvalid(f"{provider_id}: {key} must be a non-empty string")
        if not isinstance(entry["enabled"], bool):
            raise RegistryInvalid(f"{provider_id}: enabled must be true or false")

        hosts = _string_list(entry, "hosts", provider_id, allow_empty=False)
        for host in hosts:
            if host != host.lower() or not HOST_PATTERN.fullmatch(host):
                raise RegistryInvalid(f"{provider_id}: host {host!r} must be a lowercase DNS name")
            if entry["enabled"] and ("live" in host or host in KNOWN_LIVE_HOSTS):
                raise RegistryInvalid(f"{provider_id}: {host} looks like a live host and cannot be enabled")

        prefixes = _string_list(entry, "path_prefixes", provider_id, allow_empty=False)
        if not all(prefix.startswith("/") for prefix in prefixes):
            raise RegistryInvalid(f"{provider_id}: every path prefix must start with /")

        test_mode = entry["test_mode"]
        if not isinstance(test_mode, dict) or test_mode.get("rule") not in KNOWN_RULES:
            raise RegistryInvalid(f"{provider_id}: test_mode.rule must be one of {', '.join(KNOWN_RULES)}")
        if test_mode["rule"] == "key_prefix":
            allowed = test_mode.get("allowed_prefixes")
            if not isinstance(allowed, list) or not allowed or not all(
                isinstance(prefix, str) and "test" in prefix for prefix in allowed
            ):
                raise RegistryInvalid(f"{provider_id}: key_prefix needs allowed_prefixes that name test keys")

        request_headers = tuple(h.lower() for h in _string_list(entry, "forward_request_headers", provider_id, True))
        response_headers = tuple(h.lower() for h in _string_list(entry, "forward_response_headers", provider_id, True))
        for header in request_headers + response_headers:
            if not _HEADER.fullmatch(header):
                raise RegistryInvalid(f"{provider_id}: invalid header name {header!r}")
        for header in request_headers:
            if header in DROPPED_REQUEST_HEADERS or header.startswith(DROPPED_REQUEST_PREFIXES):
                raise RegistryInvalid(f"{provider_id}: {header} can never be forwarded")

        return Provider(
            id=provider_id,
            name=entry["name"],
            enabled=entry["enabled"],
            hosts=hosts,
            path_prefixes=prefixes,
            test_mode=dict(test_mode),
            forward_request_headers=request_headers,
            forward_response_headers=response_headers,
            test_key_hint=entry["test_key_hint"],
            docs_url=entry["docs_url"],
        )
    except RelayError:
        raise
    except Exception as exc:
        raise RegistryInvalid("a provider entry could not be read") from exc


def parse_providers(entries: object) -> list[Provider]:
    try:
        if not isinstance(entries, list):
            raise RegistryInvalid("the registry must be a JSON list")
        providers = [_parse_entry(entry) for entry in entries]
        seen_ids: set[str] = set()
        seen_hosts: set[str] = set()
        for provider in providers:
            if provider.id in seen_ids:
                raise RegistryInvalid(f"provider id {provider.id} appears twice")
            seen_ids.add(provider.id)
            for host in provider.hosts:
                if host in seen_hosts:
                    raise RegistryInvalid(f"host {host} appears twice")
                seen_hosts.add(host)
        return providers
    except RelayError:
        raise
    except Exception as exc:
        raise RegistryInvalid("the registry could not be read") from exc


def load_provider_registry(path: str) -> JsonProviderRegistry:
    try:
        entries = json.loads(Path(path).read_text(encoding="utf-8"))
        registry = JsonProviderRegistry(parse_providers(entries))
        logger.info("CodeLab relay providers enabled: %s",
                    ", ".join(provider.id for provider in registry.all_enabled()) or "none")
        return registry
    except RelayError:
        logger.critical("CodeLab relay registry %s is invalid", path, exc_info=True)
        raise
    except (OSError, ValueError) as exc:
        logger.critical("could not load the CodeLab relay registry %s", path, exc_info=True)
        raise RegistryInvalid(f"could not load {path}") from exc
