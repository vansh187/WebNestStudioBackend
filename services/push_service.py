import asyncio
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import google.auth.transport.requests
import httpx
from google.oauth2 import service_account

from core.config import Settings
from database.device_token_persistence import DeviceTokenPersistence
from database.messaging_persistence import MessagingPersistence
from database.models import BlogPost
from database.session import Database

logger = logging.getLogger("webnest.push")

FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
FCM_SEND_URL = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
REQUEST_TIMEOUT_SECONDS = 10.0
# Sends to one conversation's members run in parallel, but never more than this.
_SEND_CONCURRENCY = 10
_TOKEN_REFRESH_MARGIN = timedelta(minutes=5)
_TOKEN_REFRESH_TIMEOUT_SECONDS = 20.0
_TOKEN_FAILURE_BACKOFF_SECONDS = 30.0
# Android notification channels the app creates (MainApplication.kt).
CHAT_CHANNEL_ID = "chat"
BLOG_CHANNEL_ID = "blog"
_BODY_LIMIT = 180


@dataclass
class PushNotification:
    title: str
    body: str
    channel_id: str
    # FCM data values must be strings; the app routes a tap with these.
    data: dict[str, str] = field(default_factory=dict)
    # Same tag => the newer notification replaces the older one in the tray.
    tag: str | None = None


class _AccessTokenProvider:
    """Mints and caches the OAuth access token FCM's HTTP v1 API requires.

    Same shape as the Java playground's ID-token provider: the token lives
    about an hour and is reused until it has five minutes left; the refresh is
    blocking network I/O, so it runs in a worker thread under a lock."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._credentials: service_account.Credentials | None = None
        self._project_id: str | None = None
        self._cache_key: str | None = None
        self._failed_until = 0.0

    async def get(self, key_json: str) -> tuple[str, str] | None:
        """(project_id, access_token), or None when push can't be used right now."""
        cache_key = hashlib.sha256(key_json.encode("utf-8", errors="replace")).hexdigest()
        async with self._lock:
            if self._cache_key != cache_key or self._credentials is None:
                try:
                    info = json.loads(key_json)
                    self._credentials = service_account.Credentials.from_service_account_info(info, scopes=[FCM_SCOPE])
                    self._project_id = info.get("project_id")
                except Exception as exc:
                    logger.error("FIREBASE_SERVICE_ACCOUNT_JSON is not a usable service-account key: %s", type(exc).__name__)
                    self._credentials = None
                    return None
                self._cache_key = cache_key
                self._failed_until = 0.0
            credentials, project_id = self._credentials, self._project_id
            if not project_id:
                logger.error("FIREBASE_SERVICE_ACCOUNT_JSON has no project_id")
                return None

            expiry = credentials.expiry.replace(tzinfo=timezone.utc) if credentials.expiry else None
            if credentials.token and expiry and expiry - datetime.now(timezone.utc) > _TOKEN_REFRESH_MARGIN:
                return project_id, credentials.token

            if time.monotonic() < self._failed_until:
                return None
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(credentials.refresh, google.auth.transport.requests.Request()),
                    timeout=_TOKEN_REFRESH_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                # Only the type: messages can carry token-endpoint response bodies.
                self._failed_until = time.monotonic() + _TOKEN_FAILURE_BACKOFF_SECONDS
                logger.error("Could not mint an FCM access token: %s", type(exc).__name__)
                return None
            return project_id, credentials.token


_token_provider = _AccessTokenProvider()


def _is_dead_token_response(response: httpx.Response) -> bool:
    """FCM's answer for a token that will never work again (app uninstalled,
    data cleared, token rotated): 404 UNREGISTERED, or 400 naming the token."""
    if response.status_code == 404:
        return True
    if response.status_code != 400:
        return False
    try:
        error = response.json().get("error", {})
    except ValueError:
        return False
    codes = {detail.get("errorCode") for detail in error.get("details", []) if isinstance(detail, dict)}
    return "UNREGISTERED" in codes or "registration token" in str(error.get("message", "")).lower()


class PushService:
    """Sends push notifications through Firebase Cloud Messaging.

    Best-effort, like EmailService: an unconfigured key, a Google outage or a
    bad token is logged and swallowed - nothing that triggered a push (a chat
    message, a blog publish) may fail because of it."""

    def __init__(self, settings: Settings | None) -> None:
        self._key_json = (settings.firebase_service_account_json if settings is not None else "").strip()
        self._blog_topic = settings.push_blog_topic if settings is not None else "blog"

    def is_configured(self) -> bool:
        return bool(self._key_json)

    def _message(self, target: dict[str, str], notification: PushNotification) -> dict:
        android_notification: dict[str, str] = {"channel_id": notification.channel_id}
        if notification.tag:
            android_notification["tag"] = notification.tag
        return {
            "message": {
                **target,
                "notification": {"title": notification.title, "body": notification.body},
                "data": notification.data,
                "android": {"priority": "high", "notification": android_notification},
            }
        }

    async def send_to_tokens(self, tokens: list[str], notification: PushNotification) -> list[str]:
        """Returns the tokens FCM reported dead, for the caller to delete."""
        if not tokens:
            return []
        if not self.is_configured():
            logger.info("Skipping push to %d device(s): FIREBASE_SERVICE_ACCOUNT_JSON is not configured", len(tokens))
            return []
        auth = await _token_provider.get(self._key_json)
        if auth is None:
            return []
        project_id, access_token = auth
        url = FCM_SEND_URL.format(project_id=project_id)
        headers = {"Authorization": f"Bearer {access_token}"}
        semaphore = asyncio.Semaphore(_SEND_CONCURRENCY)
        dead: list[str] = []

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:

            async def _send(token: str) -> None:
                async with semaphore:
                    try:
                        response = await client.post(url, json=self._message({"token": token}, notification), headers=headers)
                    except httpx.HTTPError as exc:
                        logger.warning("FCM send failed: %s", type(exc).__name__)
                        return
                if response.status_code == 200:
                    return
                if _is_dead_token_response(response):
                    dead.append(token)
                    return
                logger.error("FCM rejected a push: HTTP %s - %s", response.status_code, response.text[:300])

            await asyncio.gather(*(_send(token) for token in tokens))
        return dead

    async def send_to_topic(self, topic: str, notification: PushNotification) -> bool:
        if not self.is_configured():
            logger.info("Skipping push to topic %r: FIREBASE_SERVICE_ACCOUNT_JSON is not configured", topic)
            return False
        auth = await _token_provider.get(self._key_json)
        if auth is None:
            return False
        project_id, access_token = auth
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    FCM_SEND_URL.format(project_id=project_id),
                    json=self._message({"topic": topic}, notification),
                    headers={"Authorization": f"Bearer {access_token}"},
                )
        except httpx.HTTPError as exc:
            logger.warning("FCM topic send failed: %s", type(exc).__name__)
            return False
        if response.status_code != 200:
            logger.error("FCM rejected a topic push: HTTP %s - %s", response.status_code, response.text[:300])
            return False
        return True

    async def notify_blog_published(self, post: BlogPost) -> bool:
        """Never raises."""
        try:
            title = (post.title or "").strip() or "A new article is live"
            body = " ".join((post.excerpt or "").split())[:_BODY_LIMIT] or "Read the latest from WebNest Studio."
            sent = await self.send_to_topic(
                self._blog_topic,
                PushNotification(
                    title=f"New on the blog: {title}",
                    body=body,
                    channel_id=BLOG_CHANNEL_ID,
                    data={"type": "blog", "slug": post.slug, "title": title},
                    tag=f"blog-{post.slug}",
                ),
            )
            if sent:
                logger.info("Sent new-blog-post push for %s", post.slug)
            return sent
        except Exception:
            logger.error("New-blog-post push for %s failed", getattr(post, "slug", "?"), exc_info=True)
            return False


async def notify_new_chat_message(
    database: Database,
    settings: Settings,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    sender_id: uuid.UUID,
) -> None:
    """Background task run after a chat message is sent: pushes it to every
    other active member's devices. Opens its own DB session (the request's is
    gone by now) and never raises."""
    # Imported here: messaging_service imports a lot, and push_service is
    # imported by blog_service, which must stay cheap to import.
    from services.messaging_service import message_preview

    push = PushService(settings)
    if not push.is_configured():
        logger.info("Skipping chat push: FIREBASE_SERVICE_ACCOUNT_JSON is not configured")
        return
    try:
        async with database.session_scope() as session:
            messaging = MessagingPersistence(session)
            conversation = await messaging.get_conversation(conversation_id)
            message = await messaging.get_message(message_id)
            if conversation is None or message is None or message.is_deleted:
                return

            sender_name = "Someone"
            recipient_ids: list[uuid.UUID] = []
            for participant in conversation.participants:
                if participant.user_id == sender_id:
                    sender_name = (participant.user.full_name or "").strip() or sender_name
                elif participant.left_at is None and participant.user is not None and participant.user.is_active:
                    recipient_ids.append(participant.user_id)
            if not recipient_ids:
                return

            devices = DeviceTokenPersistence(session)
            tokens = await devices.tokens_for_users(recipient_ids)
            if not tokens:
                return

            preview = message_preview(message.body, message.attachments) or "New message"
            if conversation.type == "group":
                title = (conversation.title or "").strip() or "Group chat"
                body = f"{sender_name}: {preview}"
            else:
                title, body = sender_name, preview
            dead = await push.send_to_tokens(
                tokens,
                PushNotification(
                    title=title,
                    body=body[:_BODY_LIMIT],
                    channel_id=CHAT_CHANNEL_ID,
                    data={
                        "type": "chat",
                        "conversation_id": str(conversation_id),
                        "message_id": str(message_id),
                        "title": title,
                    },
                    tag=f"chat-{conversation_id}",
                ),
            )
            if dead:
                await devices.delete_tokens(dead)
                logger.info("Removed %d dead device token(s)", len(dead))
    except Exception:
        logger.error("Chat push for message %s failed", message_id, exc_info=True)
