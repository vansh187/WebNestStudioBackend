"""Google Play's Android Publisher API: reading a one-time purchase and acknowledging it. Signs
its own service-account token request instead of using google-auth's blocking refresh, so no
call here holds up the event loop. Google's error text stays here: callers only see the two
errors below, and the logs only get a status code."""

import asyncio
import base64
import binascii
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import quote

import httpx
from google.auth import crypt, jwt

from core.config import Settings

logger = logging.getLogger("webnest.assistant.payments")

ANDROID_PUBLISHER_URL = "https://androidpublisher.googleapis.com/androidpublisher/v3"
SCOPE = "https://www.googleapis.com/auth/androidpublisher"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"
_ASSERTION_SECONDS = 3600
# A token is renewed this long before Google says it expires, so none is used as it lapses.
_TOKEN_MARGIN_SECONDS = 300
_TIMEOUT = httpx.Timeout(8.0, connect=4.0)
_LIMITS = httpx.Limits(max_connections=10, max_keepalive_connections=5, keepalive_expiry=60.0)
_NOT_A_PURCHASE = frozenset({400, 404, 410})
_CREDENTIALS_REFUSED = frozenset({401, 403})


_TOKEN_IN_URL = re.compile(r"(/purchases/products/[^/\s]+/tokens/)([^\s:\"']+)")


class _HidePurchaseTokens(logging.Filter):
    """httpx logs each request's URL at INFO, and a purchase URL holds the whole token. This
    leaves only the token's last 8 characters, as everywhere else."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "/tokens/" in message:
            record.msg = _TOKEN_IN_URL.sub(lambda match: match.group(1) + token_tail(match.group(2)), message)
            record.args = ()
        return True


class GooglePlayUnavailableError(Exception):
    """Our credentials were refused, or Google was down, slow or unusable. The customer may
    well have paid, so this must never be reported to them as a failed payment."""


class PlayPurchaseNotFoundError(Exception):
    """Google says the token is not a purchase of this product."""


class PlayAcknowledgeRefusedError(GooglePlayUnavailableError):
    """Google would not acknowledge the purchase, as it does once the app has already
    acknowledged or consumed it."""


@dataclass(frozen=True)
class PlayPurchase:
    """The fields of Google's ProductPurchase that decide a grant."""

    purchase_state: int | None
    consumption_state: int | None
    acknowledgement_state: int | None
    order_id: str | None
    account_id: str | None
    region_code: str | None
    purchase_time: datetime | None
    purchase_type: int | None


@dataclass(frozen=True)
class ServiceAccount:
    email: str
    private_key: str
    private_key_id: str | None
    token_uri: str


class GooglePlayClient(Protocol):
    @property
    def configured(self) -> bool: ...

    async def get_purchase(self, product_id: str, token: str) -> PlayPurchase: ...

    async def acknowledge(self, product_id: str, token: str) -> None: ...

    async def aclose(self) -> None: ...


def parse_service_account(raw: str) -> ServiceAccount | None:
    """The key as base64, the way it is stored in Render, or as the JSON itself. None when
    it can't be used, so a bad setting turns the endpoint off instead of failing each call."""
    text = raw.strip()
    if not text:
        return None
    if not text.startswith("{"):
        try:
            text = base64.b64decode(text, validate=True).decode("utf-8")
        except (binascii.Error, ValueError):
            return None
    try:
        info = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(info, dict):
        return None
    email, private_key = info.get("client_email"), info.get("private_key")
    if not isinstance(email, str) or not email or not isinstance(private_key, str) or not private_key:
        return None
    key_id = info.get("private_key_id")
    token_uri = info.get("token_uri")
    return ServiceAccount(
        email=email,
        private_key=private_key,
        private_key_id=key_id if isinstance(key_id, str) and key_id else None,
        token_uri=token_uri if isinstance(token_uri, str) and token_uri.startswith("https://") else DEFAULT_TOKEN_URI,
    )


def _int(value: Any) -> int | None:
    """Google sends some numbers as strings, such as purchaseTimeMillis."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value and "\x00" not in value else None


def _time_from_millis(value: Any) -> datetime | None:
    """None for a time that isn't one, such as a number too big for a date."""
    millis = _int(value)
    if millis is None:
        return None
    try:
        return datetime.fromtimestamp(millis / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _purchase(body: dict[str, Any]) -> PlayPurchase:
    return PlayPurchase(
        purchase_state=_int(body.get("purchaseState")),
        consumption_state=_int(body.get("consumptionState")),
        acknowledgement_state=_int(body.get("acknowledgementState")),
        order_id=_str(body.get("orderId")),
        account_id=_str(body.get("obfuscatedExternalAccountId")),
        region_code=_str(body.get("regionCode")),
        purchase_time=_time_from_millis(body.get("purchaseTimeMillis")),
        purchase_type=_int(body.get("purchaseType")),
    )


def token_tail(token: str) -> str:
    """The only part of a purchase token that may be logged."""
    return f"...{token[-8:]}"


logging.getLogger("httpx").addFilter(_HidePurchaseTokens())


class HttpGooglePlayClient:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        api_url: str = ANDROID_PUBLISHER_URL,
        clock: Any = time.time,
    ) -> None:
        self._package = settings.assistant_google_play_package_name.strip()
        self._account = parse_service_account(settings.assistant_google_play_service_account_json)
        if settings.assistant_google_play_service_account_json.strip() and self._account is None:
            logger.error("ASSISTANT_GOOGLE_PLAY_SERVICE_ACCOUNT_JSON is set but is not a usable service-account key")
        self._transport = transport
        self._api_url = api_url.rstrip("/")
        self._clock = clock
        self._client: httpx.AsyncClient | None = None
        self._signer: crypt.RSASigner | None = None
        self._access_token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return self._account is not None and bool(self._package)

    async def get_purchase(self, product_id: str, token: str) -> PlayPurchase:
        response = await self._call("GET", self._purchase_path(product_id, token))
        if response.status_code in _NOT_A_PURCHASE:
            raise PlayPurchaseNotFoundError(f"status {response.status_code}")
        self._raise_for_status(response, "read a purchase")
        try:
            body = response.json()
        except ValueError as exc:
            raise GooglePlayUnavailableError("not json") from exc
        if not isinstance(body, dict):
            raise GooglePlayUnavailableError("not an object")
        return _purchase(body)

    async def acknowledge(self, product_id: str, token: str) -> None:
        response = await self._call("POST", f"{self._purchase_path(product_id, token)}:acknowledge", {})
        if 400 <= response.status_code < 500 and response.status_code not in _CREDENTIALS_REFUSED:
            logger.info("Google Play would not acknowledge a purchase: status=%s", response.status_code)
            raise PlayAcknowledgeRefusedError(f"status {response.status_code}")
        self._raise_for_status(response, "acknowledge a purchase")

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    def _purchase_path(self, product_id: str, token: str) -> str:
        return (
            f"/applications/{quote(self._package, safe='')}/purchases/products/"
            f"{quote(product_id, safe='')}/tokens/{quote(token, safe='')}"
        )

    def _raise_for_status(self, response: httpx.Response, action: str) -> None:
        if response.status_code in _CREDENTIALS_REFUSED:
            # Wrong key, API not enabled, or the account lacks its Play Console permissions.
            logger.error("Google Play refused our service account when trying to %s: status=%s", action, response.status_code)
            raise GooglePlayUnavailableError(f"status {response.status_code}")
        if response.status_code >= 300:
            logger.warning("Google Play could not %s: status=%s", action, response.status_code)
            raise GooglePlayUnavailableError(f"status {response.status_code}")

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=_TIMEOUT, limits=_LIMITS, transport=self._transport)
        return self._client

    async def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> httpx.Response:
        response = await self._send(method, path, body)
        if response.status_code == 401:
            # The cached token was revoked or expired early, so try once more with a fresh one.
            # A second 401 means the key itself is refused.
            self._access_token = None
            response = await self._send(method, path, body)
            if response.status_code == 401:
                self._access_token = None
        return response

    async def _send(self, method: str, path: str, body: dict[str, Any] | None) -> httpx.Response:
        token = await self._token()
        try:
            return await self._get_client().request(method, self._api_url + path, json=body, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as exc:
            logger.warning("Google Play request failed: %s", type(exc).__name__)
            raise GooglePlayUnavailableError("unreachable") from exc

    async def _token(self) -> str:
        if self._access_token is not None and self._clock() < self._token_expires_at:
            return self._access_token
        async with self._token_lock:
            # Another request may have renewed it while this one waited.
            if self._access_token is not None and self._clock() < self._token_expires_at:
                return self._access_token
            self._access_token, self._token_expires_at = await self._new_token()
            return self._access_token

    async def _new_token(self) -> tuple[str, float]:
        account = self._account
        if account is None:
            raise GooglePlayUnavailableError("not configured")
        now = int(self._clock())
        claims = {"iss": account.email, "scope": SCOPE, "aud": account.token_uri, "iat": now, "exp": now + _ASSERTION_SECONDS}
        assertion = jwt.encode(self._get_signer(account), claims).decode("ascii")
        try:
            response = await self._get_client().post(account.token_uri, data={"grant_type": _GRANT_TYPE, "assertion": assertion})
        except httpx.HTTPError as exc:
            logger.warning("Google token request failed: %s", type(exc).__name__)
            raise GooglePlayUnavailableError("token unreachable") from exc
        if response.status_code != 200:
            logger.error("Google refused the service-account token request: status=%s", response.status_code)
            raise GooglePlayUnavailableError(f"token status {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise GooglePlayUnavailableError("token not json") from exc
        access_token = body.get("access_token") if isinstance(body, dict) else None
        lifetime = _int(body.get("expires_in")) if isinstance(body, dict) else None
        if not isinstance(access_token, str) or not access_token:
            raise GooglePlayUnavailableError("no access token")
        return access_token, self._clock() + max((lifetime or _ASSERTION_SECONDS) - _TOKEN_MARGIN_SECONDS, 0)

    def _get_signer(self, account: ServiceAccount) -> crypt.RSASigner:
        if self._signer is None:
            try:
                self._signer = crypt.RSASigner.from_service_account_info(
                    {"private_key": account.private_key, "private_key_id": account.private_key_id}
                )
            except (ValueError, TypeError) as exc:
                logger.error("The Google Play service-account private key could not be read")
                raise GooglePlayUnavailableError("bad private key") from exc
        return self._signer
