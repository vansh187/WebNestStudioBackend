import logging
import re

import httpx

from core.config import Settings

logger = logging.getLogger("webnest.deploy_hook")

_URL = re.compile(r"https?://[^\s\"']+")


def _clean_hook_url(raw: object) -> str:
    """The hook URL as configured, forgiving the usual paste mistakes: a
    host dashboard keeps surrounding quotes literally, and Vercel shows the
    hook as a whole `curl -X POST <url>` command. Returns "" (hook disabled)
    when there is no URL in the value at all."""
    if not isinstance(raw, str):
        return ""
    match = _URL.search(raw)
    return match.group(0) if match else ""


class DeployHookService:
    """Asks Vercel to rebuild the pre-rendered frontend. The frontend only
    learns about a new, edited or unpublished blog post at build time, so
    without this a fresh post 404s (and a removed one lingers) until someone
    redeploys by hand."""

    def __init__(self, settings: Settings | None) -> None:
        self._url = _clean_hook_url(settings.vercel_deploy_hook_url if settings is not None else "")
        self._timeout = settings.vercel_deploy_hook_timeout_seconds if settings is not None else 10.0

    async def trigger(self, reason: str) -> bool:
        """Returns whether the hook accepted the request. Never raises: a
        failed rebuild request must not fail the publish that caused it."""
        if not self._url:
            logger.info("VERCEL_DEPLOY_HOOK_URL is not configured - skipping frontend rebuild (%s)", reason)
            return False
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(self._url)
            response.raise_for_status()
        except Exception as exc:
            # The hook URL is itself a secret - log the error type only, never
            # the exception text (httpx includes the full URL in it).
            logger.error("Vercel deploy hook call failed (%s): %s", reason, type(exc).__name__)
            return False
        logger.info("Triggered a frontend rebuild via the Vercel deploy hook (%s)", reason)
        return True
