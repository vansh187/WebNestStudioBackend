"""GET /api/codelab/relay/providers (section 2.1): enabled providers only, so
the frontend never hard-codes hosts."""

import logging

from codelab_relay.constants import RELAY_PATH
from codelab_relay.errors import RelayDisabled, RelayError
from codelab_relay.protocols import ProviderRegistry
from codelab_relay.schemas import ProviderSummary, ProvidersResponse, RelayLimits
from codelab_relay.settings import RelaySettings

logger = logging.getLogger("webnest.codelab_relay")


class ProviderListService:
    def __init__(self, settings: RelaySettings, registry: ProviderRegistry) -> None:
        self._settings = settings
        self._registry = registry

    def describe(self) -> ProvidersResponse:
        try:
            providers = []
            if self._settings.enabled:
                providers = [
                    ProviderSummary(
                        id=provider.id,
                        name=provider.name,
                        hosts=list(provider.hosts),
                        test_mode=provider.test_mode["rule"],
                        test_key_hint=provider.test_key_hint,
                        docs_url=provider.docs_url,
                    )
                    for provider in self._registry.all_enabled()
                ]
            return ProvidersResponse(
                enabled=self._settings.enabled,
                relay_path=RELAY_PATH,
                limits=RelayLimits(
                    per_minute=self._settings.per_minute,
                    per_day=self._settings.per_day,
                    max_request_bytes=self._settings.max_request_bytes,
                    max_response_bytes=self._settings.max_response_bytes,
                    timeout_seconds=self._settings.timeout_seconds,
                ),
                providers=providers,
            )
        except RelayError:
            raise
        except Exception as exc:
            logger.exception("provider list failed")
            raise RelayDisabled(temporary=True) from exc
