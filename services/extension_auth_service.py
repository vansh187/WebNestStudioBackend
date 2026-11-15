"""Logging VS Code in with a WebNest account: OAuth 2.0 authorization code with PKCE (RFC 7636).

The website, with its own login, asks for a one-time code bound to the extension's PKCE
challenge and redirect URI. The extension swaps the code and its verifier for a 15-minute
access token (scope "extension", tied to a session) and a refresh token that rotates on every
use. Each refusal is an ExtensionError with the contract's code; a code is spent before any
check on it, so a failed attempt can never be retried with the same code."""

import base64
import hashlib
import hmac
import logging
import re
import secrets
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlsplit

from core.config import Settings
from core.exceptions import DomainError, ExtensionError
from core.security import EXTENSION_SCOPE, JWTHandler
from database.extension_session_persistence import (
    ExtensionSessionStore,
    NewAuthCode,
    NewSession,
    SessionSummary,
    SessionUser,
)
from schemas.extension_schemas import CodeRequest, CodeResponse, SessionList, SessionOut, TokenRequest, TokenResponse

logger = logging.getLogger("webnest.extension.auth")

EXTENSION_ID = "webneststudio.webnest-ai"
CODE_PREFIX = "wn_ac_"
REFRESH_PREFIX = "wn_rt_"
CODE_BYTES = 32
REFRESH_BYTES = 48
CODE_SECONDS = 300
# A refresh token used again within this long still works (two windows refreshing at once,
# a response lost on a flaky network); used again later, it was copied.
REFRESH_REUSE_GRACE = timedelta(seconds=30)
# Ended sessions and spent codes and tokens are kept this long, then purged.
EXPIRED_KEEP = timedelta(days=1)
ENDED_SESSION_KEEP = timedelta(days=30)

# The editors the extension runs in: the redirect scheme and the name shown on the website.
APP_NAMES: dict[str, str] = {
    "vscode": "Visual Studio Code",
    "vscode-insiders": "VS Code Insiders",
    "vscodium": "VSCodium",
    "cursor": "Cursor",
    "windsurf": "Windsurf",
    "positron": "Positron",
}
_CALLBACK_PATH = "/auth"
_WINDOW_ID = "windowId"
_WINDOW_ID_VALUE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_REDIRECT_CHARS = 2000
# RFC 7636 section 4.1: 43 to 128 unreserved characters.
_VERIFIER = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
_CLIENT_VERSION = re.compile(r"^\s*[A-Za-z][A-Za-z0-9_-]*/v?(\d{1,6})\.(\d{1,6})\.(\d{1,6})")
_VERSION = re.compile(r"^\s*v?(\d{1,6})\.(\d{1,6})\.(\d{1,6})")

_INVALID_CODE = "This login code is not valid or has expired. Please log in again."
_INVALID_VERIFIER = "This login could not be verified. Please log in again."
_INVALID_REDIRECT = "This login link is not for WebNest AI in VS Code. Please log in again."
_TOKEN_EXPIRED = "Your access token expired. Refresh it and try again."
_NOT_AUTHENTICATED = "Please log in to WebNest AI."
_SESSION_EXPIRED = "Your WebNest AI session ended. Please log in again."
_SESSION_REVOKED = "You were logged out of WebNest AI. Please log in again."
_ACCOUNT_DISABLED = "This account is disabled. Please contact support."
_LOGIN_FAILED = "Could not finish logging in. Please try again."
_SESSION_NOT_FOUND = "That VS Code connection was not found."


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()


def new_secret(prefix: str, nbytes: int) -> str:
    return prefix + secrets.token_urlsafe(nbytes)


def pkce_s256(verifier: str) -> str:
    """base64url(sha256(verifier)) without padding, as RFC 7636 defines S256."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def pkce_matches(verifier: str, challenge: str) -> bool:
    """Constant time, and False for anything RFC 7636 wouldn't accept as a verifier."""
    if not _VERIFIER.match(verifier):
        return False
    return hmac.compare_digest(pkce_s256(verifier).encode("ascii"), challenge.encode("ascii", "replace"))


def app_name_for(scheme: str | None) -> str | None:
    """The display name for a known editor, else the value as sent (already cut to 30)."""
    if not scheme:
        return None
    return APP_NAMES.get(scheme.strip().lower(), scheme)


def is_allowed_redirect(uri: str) -> bool:
    """<scheme>://webneststudio.webnest-ai/auth for a known editor's scheme, with no query but
    one windowId, no user, port or fragment. Anything else could send a code to another app."""
    if not uri or len(uri) > _MAX_REDIRECT_CHARS or any(ord(char) <= 32 or ord(char) == 127 for char in uri):
        return False
    try:
        parts = urlsplit(uri)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme not in APP_NAMES or port is not None or "@" in parts.netloc or parts.fragment or "#" in uri:
        return False
    if parts.netloc.lower() != EXTENSION_ID or parts.path != _CALLBACK_PATH:
        return False
    if not parts.query:
        return "?" not in uri
    try:
        pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return False
    return len(pairs) == 1 and pairs[0][0] == _WINDOW_ID and bool(_WINDOW_ID_VALUE.match(pairs[0][1]))


def parse_version(text: str | None) -> tuple[int, int, int] | None:
    if not text:
        return None
    match = _VERSION.match(text)
    return None if match is None else (int(match[1]), int(match[2]), int(match[3]))


def client_version(header: str | None) -> tuple[int, int, int] | None:
    """The version in X-WebNest-Client ("vscode/0.1.0 (Visual Studio Code)"), or None."""
    if not header:
        return None
    match = _CLIENT_VERSION.match(header)
    return None if match is None else (int(match[1]), int(match[2]), int(match[3]))


def version_text(version: tuple[int, int, int] | None) -> str | None:
    return None if version is None else ".".join(str(part) for part in version)


def _session_out(summary: SessionSummary, current: uuid.UUID | None) -> SessionOut:
    return SessionOut(
        id=summary.session_id,
        device_name=summary.device_name,
        app_name=summary.app_name,
        ext_version=summary.ext_version,
        created_at=summary.created_at,
        last_used_at=summary.last_used_at,
        expires_at=summary.expires_at,
        current=current is not None and summary.session_id == current,
    )


class ExtensionAuthService:
    def __init__(
        self,
        settings: Settings,
        store: ExtensionSessionStore,
        jwt_handler: JWTHandler,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._settings = settings
        self._store = store
        self._jwt = jwt_handler
        self._clock = clock

    async def create_code(self, user_id: uuid.UUID, payload: CodeRequest) -> CodeResponse:
        """2.1: called by the website once the user clicks Connect."""
        if not is_allowed_redirect(payload.redirect_uri):
            raise ExtensionError(400, "invalid_redirect_uri", _INVALID_REDIRECT)
        code = new_secret(CODE_PREFIX, CODE_BYTES)
        await self._store.create_code(
            NewAuthCode(
                code_hash=hash_secret(code),
                user_id=user_id,
                code_challenge=payload.code_challenge,
                redirect_uri=payload.redirect_uri,
                device_name=payload.device_name,
                app_name=app_name_for(payload.app_scheme),
                ext_version=payload.ext_version,
                expires_at=self._clock() + timedelta(seconds=CODE_SECONDS),
            )
        )
        logger.info("VS Code login code issued: user=%s", user_id)
        return CodeResponse(code=code, expires_in=CODE_SECONDS)

    async def exchange(self, payload: TokenRequest) -> TokenResponse:
        """2.3. The code is spent first, so it is burned even when a later check fails."""
        if not payload.code.startswith(CODE_PREFIX):
            raise ExtensionError(400, "invalid_code", _INVALID_CODE)
        now = self._clock()
        consumed = await self._store.consume_code(hash_secret(payload.code), now)
        if consumed is None:
            raise ExtensionError(400, "invalid_code", _INVALID_CODE)
        # Byte for byte, as RFC 6749 requires for a redirect URI.
        if not hmac.compare_digest(consumed.redirect_uri.encode("utf-8"), payload.redirect_uri.encode("utf-8", "replace")):
            raise ExtensionError(400, "invalid_redirect_uri", _INVALID_REDIRECT)
        if not pkce_matches(payload.code_verifier, consumed.code_challenge):
            logger.warning("VS Code login refused, PKCE check failed: user=%s", consumed.user_id)
            raise ExtensionError(400, "invalid_code_verifier", _INVALID_VERIFIER)
        refresh_token = new_secret(REFRESH_PREFIX, REFRESH_BYTES)
        session_expires_at = now + timedelta(days=max(self._settings.extension_session_days, 1))
        token_expires_at = min(session_expires_at, now + timedelta(days=max(self._settings.extension_refresh_token_days, 1)))
        opened = await self._store.open_session(
            NewSession(
                user_id=consumed.user_id,
                device_name=consumed.device_name,
                app_name=consumed.app_name,
                ext_version=consumed.ext_version,
                expires_at=session_expires_at,
                token_hash=hash_secret(refresh_token),
                token_expires_at=token_expires_at,
            ),
            now,
        )
        if opened.status == "disabled":
            raise ExtensionError(403, "account_disabled", _ACCOUNT_DISABLED)
        if opened.status != "ok" or opened.session_id is None:
            raise ExtensionError(400, "invalid_code", _INVALID_CODE)
        logger.info("VS Code connected: user=%s session=%s", consumed.user_id, opened.session_id)
        return self._tokens(consumed.user_id, opened.role or "client", opened.session_id, refresh_token, token_expires_at)

    async def refresh(self, refresh_token: str, client_header: str | None) -> TokenResponse:
        """2.4: a new pair, the same session_id, and the session's end never moves."""
        if not refresh_token.startswith(REFRESH_PREFIX):
            raise ExtensionError(401, "session_revoked", _SESSION_REVOKED)
        new_token = new_secret(REFRESH_PREFIX, REFRESH_BYTES)
        result = await self._store.refresh(
            hash_secret(refresh_token),
            hash_secret(new_token),
            self._clock(),
            REFRESH_REUSE_GRACE,
            timedelta(days=max(self._settings.extension_refresh_token_days, 1)),
            version_text(client_version(client_header)),
        )
        if result.status == "disabled":
            raise ExtensionError(403, "account_disabled", _ACCOUNT_DISABLED)
        if result.status == "expired":
            raise ExtensionError(401, "session_expired", _SESSION_EXPIRED)
        if result.status == "reused":
            logger.warning("VS Code refresh token used again after the grace period; session revoked: session=%s", result.session_id)
        if result.status != "ok" or result.session_id is None or result.user_id is None or result.token_expires_at is None:
            raise ExtensionError(401, "session_revoked", _SESSION_REVOKED)
        return self._tokens(result.user_id, result.role or "client", result.session_id, new_token, result.token_expires_at)

    async def logout(self, refresh_token: str) -> None:
        """2.5. Never fails: the extension has already signed out locally."""
        if not refresh_token.startswith(REFRESH_PREFIX):
            return
        try:
            await self._store.revoke_by_token(hash_secret(refresh_token), self._clock(), "logout")
        except DomainError as exc:
            logger.error("Could not end a VS Code session at logout: %s", type(exc).__name__)

    async def authenticate(self, access_token: str) -> SessionUser:
        """Every extension request: a valid extension token, and a live session of an active
        account, read fresh, so a disconnect on the website takes effect at once."""
        claims, reason = self._jwt.decode_token_or_reason(access_token)
        if reason == "expired":
            raise ExtensionError(401, "token_expired", _TOKEN_EXPIRED)
        if claims is None or claims.get("type") != "access" or claims.get("scope") != EXTENSION_SCOPE:
            raise ExtensionError(401, "not_authenticated", _NOT_AUTHENTICATED)
        try:
            user_id = uuid.UUID(str(claims["sub"]))
            session_id = uuid.UUID(str(claims["sid"]))
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            raise ExtensionError(401, "not_authenticated", _NOT_AUTHENTICATED) from exc
        session_user = await self._store.session_user(session_id)
        if session_user is None or session_user.user_id != user_id:
            raise ExtensionError(401, "session_revoked", _SESSION_REVOKED)
        if not session_user.user_active:
            raise ExtensionError(403, "account_disabled", _ACCOUNT_DISABLED)
        if session_user.revoked:
            raise ExtensionError(401, "session_revoked", _SESSION_REVOKED)
        if session_user.expires_at <= self._clock():
            raise ExtensionError(401, "session_expired", _SESSION_EXPIRED)
        return session_user

    async def list_sessions(self, user_id: uuid.UUID, current: uuid.UUID | None = None) -> SessionList:
        """2.9. With the website's token there is no current session, so all are false."""
        summaries = await self._store.list_sessions(user_id, self._clock())
        return SessionList(sessions=[_session_out(summary, current) for summary in summaries])

    async def disconnect(self, user_id: uuid.UUID, session_id: str) -> None:
        try:
            parsed = uuid.UUID(session_id.strip())
        except (ValueError, AttributeError) as exc:
            raise ExtensionError(404, "not_found", _SESSION_NOT_FOUND) from exc
        if not await self._store.revoke_session(user_id, parsed, self._clock(), "user"):
            raise ExtensionError(404, "not_found", _SESSION_NOT_FOUND)
        logger.info("VS Code disconnected from the website: user=%s session=%s", user_id, parsed)

    async def purge(self) -> int:
        now = self._clock()
        return await self._store.purge(now - EXPIRED_KEEP, now - ENDED_SESSION_KEEP)

    def _tokens(
        self, user_id: uuid.UUID, role: str, session_id: uuid.UUID, refresh_token: str, refresh_expires_at: datetime
    ) -> TokenResponse:
        minutes = max(self._settings.extension_access_token_minutes, 1)
        try:
            access_token = self._jwt.create_extension_access_token(str(user_id), role, str(session_id), minutes)
        except ValueError as exc:
            raise ExtensionError(500, "internal_error", _LOGIN_FAILED) from exc
        return TokenResponse(
            access_token=access_token,
            access_expires_in=minutes * 60,
            refresh_token=refresh_token,
            refresh_expires_at=refresh_expires_at,
            session_id=session_id,
        )
