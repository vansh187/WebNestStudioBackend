"""Per-path CORS policies. Each (prefix, options) pair gets its own Starlette
CORSMiddleware; any other path uses the default policy. Used to give the
CodeLab relay and database proxy strict policies (explicit origins and
headers, no credentials, their error headers exposed) while the rest of the
API keeps the site-wide one."""

import logging

from starlette.middleware.cors import CORSMiddleware

logger = logging.getLogger("webnest.cors")


def is_under(path: str, prefix: str) -> bool:
    """True for the prefix itself and anything below it, not for look-alikes
    such as /api/codelab/dbx."""
    try:
        return path == prefix or path.startswith(prefix + "/")
    except (AttributeError, TypeError):
        return False


class PathScopedCORSMiddleware:
    def __init__(self, app, scoped: tuple[tuple[str, dict], ...], default_options: dict) -> None:
        self._scoped = [(prefix, CORSMiddleware(app, **options)) for prefix, options in scoped]
        self._default = CORSMiddleware(app, **default_options)

    async def __call__(self, scope, receive, send) -> None:
        try:
            handler = self._default
            if scope.get("type") == "http":
                path = str(scope.get("path", ""))
                handler = next((cors for prefix, cors in self._scoped if is_under(path, prefix)), self._default)
            await handler(scope, receive, send)
        except Exception:
            # Errors from the app below belong to it; log the hop, never swallow.
            logger.debug("request failed below the CORS layer", exc_info=True)
            raise
