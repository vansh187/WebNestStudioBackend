import asyncio
import logging
import re
import time
import uuid
from datetime import datetime, timezone

import httpx

from core.config import Settings
from core.exceptions import ExternalServiceError, PayloadTooLargeError, UnsupportedMediaTypeError

logger = logging.getLogger("webnest.storage")

REQUEST_TIMEOUT_SECONDS = 10.0
UPLOAD_URL_TTL_SECONDS = 120
DOWNLOAD_URL_TTL_SECONDS = 3600
# Serve a previously minted signed GET URL from memory until this many seconds
# before it actually expires, so a message list re-fetched inside the TTL does
# no Storage round-trips at all.
DOWNLOAD_URL_CACHE_MARGIN_SECONDS = 300
DOWNLOAD_URL_CACHE_MAX_ENTRIES = 5000
# Bounds on the shared connection pool to Supabase Storage.
_MAX_CONNECTIONS = 20
_MAX_KEEPALIVE = 10

# MIME allowlist - section 4 of the chat backend spec.
ALLOWED_MIME_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/gif",
        "image/heic",
        "application/pdf",
        "text/plain",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/zip",
    }
)

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class StorageService:
    """Talks to Supabase Storage's REST API (private bucket, service-role key)
    to mint short-lived signed upload and download URLs for chat attachments.

    The app never gets a storage credential: it asks this service for a signed
    upload URL, PUTs the bytes straight to Supabase, then references the
    returned url_path on the message; on every read the backend mints a fresh
    signed GET URL. Constructed once at startup (stateless, like EmailService).
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # One pooled client for the whole process, created lazily inside the
        # event loop (never at import time) and closed on app shutdown. Opening
        # a fresh AsyncClient per call meant a new TCP+TLS handshake for every
        # attachment on every message-list read.
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        # url_path -> (absolute_signed_url, monotonic_expiry).
        self._download_cache: dict[str, tuple[str, float]] = {}

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def _get_client(self) -> httpx.AsyncClient:
        client = self._client
        if client is not None and not client.is_closed:
            return client
        async with self._client_lock:
            if self._client is None or self._client.is_closed:
                self._client = httpx.AsyncClient(
                    timeout=REQUEST_TIMEOUT_SECONDS,
                    limits=httpx.Limits(
                        max_connections=_MAX_CONNECTIONS,
                        max_keepalive_connections=_MAX_KEEPALIVE,
                    ),
                )
            return self._client

    async def aclose(self) -> None:
        """Close the shared client. Best-effort - called from app shutdown."""
        client = self._client
        self._client = None
        self._download_cache.clear()
        if client is not None and not client.is_closed:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001 - shutdown must never raise
                logger.warning("Error closing the Supabase Storage client", exc_info=True)

    # ------------------------------------------------------------------ #
    # Configuration / validation
    # ------------------------------------------------------------------ #
    def is_configured(self) -> bool:
        return bool(
            self._settings.supabase_project_url.strip()
            and self._settings.supabase_service_role_key.strip()
        )

    def max_attachment_bytes(self) -> int:
        return max(self._settings.chat_max_attachment_mb, 1) * 1024 * 1024

    def derive_kind(self, mime_type: str) -> str:
        normalized = (mime_type or "").strip().lower()
        if normalized.startswith("image/"):
            return "image"
        if normalized == "application/pdf":
            return "pdf"
        return "file"

    def validate_descriptor(self, mime_type: str, size_bytes: int) -> None:
        """Raises the spec's 415 / 413 for a disallowed type or oversize file."""
        normalized = (mime_type or "").strip().lower()
        if normalized not in ALLOWED_MIME_TYPES:
            raise UnsupportedMediaTypeError("File type not allowed")
        if size_bytes < 0:
            raise PayloadTooLargeError("File size is invalid")
        if size_bytes > self.max_attachment_bytes():
            raise PayloadTooLargeError(
                f"File exceeds the {self._settings.chat_max_attachment_mb} MB limit"
            )

    def sanitize_filename(self, filename: str) -> str:
        base = (filename or "").strip().replace("\\", "/").split("/")[-1]
        cleaned = _SAFE_NAME_RE.sub("_", base).strip("._")
        if not cleaned:
            cleaned = "file"
        return cleaned[:128]

    def build_object_path(self, user_id: uuid.UUID, filename: str) -> str:
        now = datetime.now(timezone.utc)
        return f"{user_id}/{now:%Y}/{now:%m}/{uuid.uuid4().hex}-{self.sanitize_filename(filename)}"

    def owns_object_path(self, user_id: uuid.UUID, url_path: str) -> bool:
        """True only if url_path is one this user could have been issued by
        sign_upload - i.e. it sits under their own ``{user_id}/`` prefix and
        contains no traversal. The client is never trusted to hand back a path
        pointing at someone else's uploads."""
        if not url_path or url_path.startswith("/") or "\\" in url_path:
            return False
        segments = url_path.split("/")
        if any(segment in ("", ".", "..") for segment in segments):
            return False
        return segments[0] == str(user_id)

    # ------------------------------------------------------------------ #
    # Signing
    # ------------------------------------------------------------------ #
    async def sign_upload(
        self, user_id: uuid.UUID, filename: str, mime_type: str, size_bytes: int
    ) -> dict:
        self.validate_descriptor(mime_type, size_bytes)
        self._require_configured()
        bucket = self._settings.chat_storage_bucket
        object_path = self.build_object_path(user_id, filename)
        payload = await self._post(
            f"/object/upload/sign/{bucket}/{object_path}", json_body=None
        )
        signed = payload.get("url")
        if not isinstance(signed, str) or not signed:
            logger.error("Supabase upload-sign response missing 'url': %s", payload)
            raise ExternalServiceError("Storage service returned an unusable response")
        return {
            "url_path": object_path,
            "storage_path": f"{bucket}/{object_path}",
            "upload_url": self._absolute(signed),
            "method": "PUT",
            "headers": {"Content-Type": mime_type, "x-upsert": "true"},
            "expires_in": UPLOAD_URL_TTL_SECONDS,
        }

    async def sign_download(
        self, url_path: str, expires_in: int = DOWNLOAD_URL_TTL_SECONDS
    ) -> str | None:
        """Best-effort: returns a fresh signed GET URL, or None if storage is
        unconfigured / unreachable / rejects the path. Never raises - it runs
        once per attachment while serialising a message list and must not be
        able to 500 that response."""
        if not self.is_configured() or not url_path:
            return None

        now = time.monotonic()
        cached = self._download_cache.get(url_path)
        if cached is not None and cached[1] > now:
            return cached[0]

        bucket = self._settings.chat_storage_bucket
        try:
            payload = await self._post(
                f"/object/sign/{bucket}/{url_path}", json_body={"expiresIn": expires_in}
            )
        except ExternalServiceError:
            return None
        signed = payload.get("signedURL") or payload.get("signedUrl")
        if not isinstance(signed, str) or not signed:
            logger.warning("Supabase download-sign response missing 'signedURL': %s", payload)
            return None

        absolute = self._absolute(signed)
        ttl = max(expires_in - DOWNLOAD_URL_CACHE_MARGIN_SECONDS, 60)
        self._remember_download_url(url_path, absolute, now + ttl)
        return absolute

    def _remember_download_url(self, url_path: str, absolute: str, expiry: float) -> None:
        cache = self._download_cache
        if len(cache) >= DOWNLOAD_URL_CACHE_MAX_ENTRIES:
            now = time.monotonic()
            for stale_key in [key for key, (_, exp) in cache.items() if exp <= now]:
                cache.pop(stale_key, None)
            if len(cache) >= DOWNLOAD_URL_CACHE_MAX_ENTRIES:
                cache.clear()
        cache[url_path] = (absolute, expiry)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _require_configured(self) -> None:
        if not self.is_configured():
            raise ExternalServiceError(
                "Attachments are not configured on this server. Set SUPABASE_PROJECT_URL "
                "and SUPABASE_SERVICE_ROLE_KEY to enable them."
            )

    def _storage_base(self) -> str:
        return self._settings.supabase_project_url.rstrip("/") + "/storage/v1"

    def _absolute(self, signed_path: str) -> str:
        if signed_path.startswith("http://") or signed_path.startswith("https://"):
            return signed_path
        return self._storage_base() + "/" + signed_path.lstrip("/")

    async def _post(self, path: str, json_body: dict | None) -> dict:
        key = self._settings.supabase_service_role_key
        headers = {"Authorization": f"Bearer {key}", "apikey": key}
        url = self._storage_base() + path
        try:
            client = await self._get_client()
            response = await client.post(url, json=json_body or {}, headers=headers)
        except httpx.TimeoutException as exc:
            logger.warning("Supabase Storage request timed out: %s", url)
            raise ExternalServiceError("Storage service did not respond in time") from exc
        except httpx.HTTPError as exc:
            logger.warning("Supabase Storage request failed: %s (%s)", url, exc)
            raise ExternalServiceError("Storage service is temporarily unavailable") from exc

        if response.status_code in (401, 403):
            logger.error("Supabase Storage rejected the service-role key (HTTP %s)", response.status_code)
            raise ExternalServiceError("Storage service credentials are invalid")
        if response.status_code == 404:
            raise ExternalServiceError("Storage object or bucket was not found")
        if response.status_code >= 400:
            logger.error(
                "Supabase Storage error HTTP %s for %s: %s",
                response.status_code,
                url,
                response.text[:300],
            )
            raise ExternalServiceError("Storage service returned an error")
        try:
            data = response.json()
        except ValueError as exc:
            raise ExternalServiceError("Storage service returned an invalid response") from exc
        if not isinstance(data, dict):
            raise ExternalServiceError("Storage service returned an unexpected response")
        return data
