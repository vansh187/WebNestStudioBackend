import asyncio
import logging
import time
from urllib.parse import quote

import httpx

from core.exceptions import GitHubError

logger = logging.getLogger("webnest.github")

API_BASE = "https://api.github.com"
OAUTH_TOKEN_URL = "https://github.com/login/oauth/access_token"
_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "WebNest-CodeLab",
}

# Connection problems are retried with a short backoff. A request that never
# reached GitHub (connect failure, no free pooled connection) is safe to retry
# for any method; one that may have reached it is retried only when repeating
# it changes nothing (GET/DELETE).
RETRY_DELAYS = (0.5, 1.5)
_IDEMPOTENT = {"GET", "HEAD", "DELETE"}
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
_RETRYABLE_STATUS = {502, 503, 504}


def unavailable(message: str = "GitHub is not reachable right now. Please try again.") -> GitHubError:
    return GitHubError(502, "github_unavailable", message)


def path_part(value: str) -> str:
    """URL-encodes one validated path component (owner, repo, sha)."""
    return quote(value, safe="")


def ref_part(branch: str) -> str:
    """URL-encodes a validated branch name, keeping its `/` separators."""
    return quote(branch, safe="/")


def json_object(response: httpx.Response) -> dict:
    """The response body as a dict; {} when it is not a JSON object."""
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def send_with_retry(http: httpx.AsyncClient, method: str, url: str, **kwargs) -> httpx.Response:
    """One HTTP call with retries for connection problems and GitHub 5xx
    hiccups. Anything still failing becomes 502 github_unavailable."""
    method = method.upper()
    for attempt in range(len(RETRY_DELAYS) + 1):
        last_attempt = attempt == len(RETRY_DELAYS)
        try:
            response = await http.request(method, url, **kwargs)
        except _NOT_SENT as exc:
            if last_attempt:
                logger.warning("GitHub unreachable after %s attempts: %s", attempt + 1, type(exc).__name__)
                raise unavailable() from exc
        except httpx.TimeoutException as exc:
            if last_attempt or method not in _IDEMPOTENT:
                raise unavailable("GitHub took too long to respond. Please try again.") from exc
        except httpx.HTTPError as exc:
            if last_attempt or method not in _IDEMPOTENT:
                logger.warning("GitHub request failed: %s", type(exc).__name__)
                raise unavailable() from exc
        else:
            if response.status_code in _RETRYABLE_STATUS and method in _IDEMPOTENT and not last_attempt:
                logger.info("GitHub answered %s, retrying", response.status_code)
            else:
                return response
        await asyncio.sleep(RETRY_DELAYS[attempt])
    raise unavailable()  # unreachable: the last attempt always returns or raises


def _request_method(response: httpx.Response) -> str:
    try:
        return response.request.method.upper()
    except RuntimeError:  # a Response built without a request
        return "GET"


def _retry_after(response: httpx.Response) -> int:
    header = response.headers.get("retry-after", "")
    if header.isdigit():
        return max(1, int(header))
    reset = response.headers.get("x-ratelimit-reset", "")
    if reset.isdigit():
        return max(1, min(3600, int(reset) - int(time.time())))
    return 60


def _is_rate_limited(response: httpx.Response) -> bool:
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    if response.headers.get("x-ratelimit-remaining") == "0":
        return True
    return "rate limit" in str(json_object(response).get("message", "")).lower()


def error_for(response: httpx.Response) -> GitHubError:
    """Maps a non-2xx GitHub API response to the contract's error. GitHub's
    own message is not echoed back - it can name private repositories."""
    status = response.status_code
    if status == 401:
        # Never 401: that would make the frontend log the user out of WebNest.
        return GitHubError(
            409, "github_reauth_required", "Your GitHub connection has expired. Please connect GitHub again."
        )
    if _is_rate_limited(response):
        retry_after = _retry_after(response)
        return GitHubError(
            429, "github_rate_limited", f"GitHub rate limit reached. Try again in {retry_after} s.", retry_after=retry_after
        )
    if status == 403 and _request_method(response) not in _IDEMPOTENT:
        # Readable but not writable (read-only collaborator, protected org
        # settings, token without the needed scope).
        return GitHubError(
            404, "repo_not_found", "You don't have permission to make this change on GitHub. Check your access to this repository."
        )
    if status in (403, 404, 409):
        # 403 = no access, 409 = empty repository: both mean there is nothing
        # this user can read or write there.
        return GitHubError(404, "repo_not_found", "Repository or branch not found, or you don't have access to it.")
    if status == 422:
        return GitHubError(422, None, "GitHub rejected this request.")
    logger.warning("GitHub API answered %s", status)
    return unavailable()


class GitHubClient:
    """Thin async client for the GitHub REST API, scoped to one access token.
    Use as `async with GitHubClient(token, timeout) as gh:`."""

    def __init__(self, token: str, timeout: float, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._token = token
        self._timeout = timeout
        self._transport = transport  # tests only
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "GitHubClient":
        self._http = httpx.AsyncClient(
            base_url=API_BASE,
            timeout=self._timeout,
            follow_redirects=False,
            headers={**_HEADERS, "Authorization": f"Bearer {self._token}"},
            transport=self._transport,
        )
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                logger.warning("Could not close the GitHub HTTP client cleanly", exc_info=True)

    async def send(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None) -> httpx.Response:
        """Returns the raw response for any status; connection failures become
        502 github_unavailable."""
        if self._http is None:
            raise unavailable("GitHub client is not ready. Please try again.")
        return await send_with_retry(self._http, method, path, params=params, json=json)

    async def request(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None) -> httpx.Response:
        """Like send(), but any non-2xx response raises the mapped GitHubError."""
        response = await self.send(method, path, params=params, json=json)
        if response.status_code >= 400:
            raise error_for(response)
        return response

    async def json(self, method: str, path: str, **kwargs):
        response = await self.request(method, path, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            raise unavailable("GitHub sent an unreadable response. Please try again.") from exc


async def exchange_oauth_code(
    client_id: str, client_secret: str, code: str, redirect_uri: str, timeout: float
) -> dict:
    """Trades an OAuth `code` for a token. GitHub reports a bad code with 200
    and an `error` field; that becomes 400 oauth_exchange_failed. A code is
    single-use, so only a request that never reached GitHub is retried."""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as http:
        response = await send_with_retry(
            http,
            "POST",
            OAUTH_TOKEN_URL,
            headers={"Accept": "application/json", "User-Agent": _HEADERS["User-Agent"]},
            data={"client_id": client_id, "client_secret": client_secret, "code": code, "redirect_uri": redirect_uri},
        )
    if response.status_code >= 500:
        raise unavailable()
    data = json_object(response)
    if response.status_code >= 400 or data.get("error") or not data.get("access_token"):
        logger.info("GitHub rejected an OAuth code (%s)", data.get("error") or response.status_code)
        raise GitHubError(400, "oauth_exchange_failed", "GitHub could not complete the sign-in. Please try connecting again.")
    return data


async def _revoke(kind: str, client_id: str, client_secret: str, token: str, timeout: float) -> None:
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as http:
            response = await send_with_retry(
                http,
                "DELETE",
                f"{API_BASE}/applications/{path_part(client_id)}/{kind}",
                headers=_HEADERS,
                auth=(client_id, client_secret),
                json={"access_token": token},
            )
        if response.status_code not in (204, 404, 422):
            logger.warning("GitHub %s revocation answered %s", kind, response.status_code)
    except Exception as exc:
        logger.warning("GitHub %s revocation failed: %s", kind, type(exc).__name__)


async def revoke_grant(client_id: str, client_secret: str, token: str, timeout: float) -> None:
    """Best effort: deletes the whole OAuth grant (every token of it). Never raises."""
    await _revoke("grant", client_id, client_secret, token, timeout)


async def revoke_token(client_id: str, client_secret: str, token: str, timeout: float) -> None:
    """Best effort: invalidates this one token only, leaving other tokens of
    the same grant working. Never raises."""
    await _revoke("token", client_id, client_secret, token, timeout)
