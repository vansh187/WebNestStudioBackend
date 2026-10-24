"""Section 2.4 / security fix 11: relay routes get their own strict CORS policy
(explicit origins, methods and headers, no credentials, never "*"), while
every other route keeps the site-wide one. Both are Starlette CORSMiddleware
instances wrapping the same app; this only picks one by path."""

import logging

from starlette.middleware.cors import CORSMiddleware

from codelab_relay.constants import RELAY_PATH

logger = logging.getLogger("webnest.codelab_relay")


def is_relay_path(path: str) -> bool:
    try:
        return path == RELAY_PATH or path.startswith(RELAY_PATH + "/")
    except (AttributeError, TypeError):
        return False


class RelayAwareCORSMiddleware:
    def __init__(self, app, relay_options: dict, default_options: dict) -> None:
        self._relay = CORSMiddleware(app, **relay_options)
        self._default = CORSMiddleware(app, **default_options)

    async def __call__(self, scope, receive, send) -> None:
        try:
            relay = scope.get("type") == "http" and is_relay_path(str(scope.get("path", "")))
            await (self._relay if relay else self._default)(scope, receive, send)
        except Exception:
            # Errors from the app below belong to it; log the hop, never swallow.
            logger.debug("request failed below the CORS layer", exc_info=True)
            raise
