"""Push notifications: FCM payloads, dead-token detection, and when a blog
publish or chat message triggers a push.

No network or database: httpx, the FCM access token and persistence are
replaced with in-memory fakes, as in test_blog_pipeline.py.
"""

import asyncio
import uuid
from datetime import timedelta
from types import SimpleNamespace

import httpx

from services import push_service
from services.push_service import PushNotification, PushService, notify_new_chat_message
from tests.test_blog_pipeline import NOW, draft, make_blog_service, make_generator, make_post

SETTINGS = SimpleNamespace(firebase_service_account_json='{"project_id": "webnest-test"}', push_blog_topic="blog")


def _response(status_code: int, json_body: dict | None = None) -> httpx.Response:
    return httpx.Response(status_code, json=json_body or {}, request=httpx.Request("POST", "https://fcm.test"))


def _patch_fcm(monkeypatch, responses: dict[str, httpx.Response] | None = None) -> list[dict]:
    """Records every FCM message sent; answers 200 unless `responses` maps a token/topic to something else."""
    sent: list[dict] = []
    responses = responses or {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json, headers):
            assert headers["Authorization"] == "Bearer fake-token"
            assert "/projects/webnest-test/messages:send" in url
            sent.append(json["message"])
            target = json["message"].get("token") or json["message"].get("topic")
            return responses.get(target, _response(200))

    async def fake_get(self, key_json):
        return "webnest-test", "fake-token"

    monkeypatch.setattr(push_service.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(push_service._AccessTokenProvider, "get", fake_get)
    return sent


def _note() -> PushNotification:
    return PushNotification(title="Hi", body="There", channel_id="chat", data={"type": "chat"}, tag="chat-1")


# ---- PushService ----


def test_unconfigured_push_is_a_silent_no_op(monkeypatch):
    sent = _patch_fcm(monkeypatch)
    service = PushService(SimpleNamespace(firebase_service_account_json="", push_blog_topic="blog"))
    assert asyncio.run(service.send_to_tokens(["t" * 30], _note())) == []
    assert asyncio.run(service.send_to_topic("blog", _note())) is False
    assert sent == []


def test_message_targets_the_android_channel_with_high_priority(monkeypatch):
    sent = _patch_fcm(monkeypatch)
    asyncio.run(PushService(SETTINGS).send_to_tokens(["device-a"], _note()))
    (message,) = sent
    assert message["token"] == "device-a"
    assert message["notification"] == {"title": "Hi", "body": "There"}
    assert message["android"] == {"priority": "high", "notification": {"channel_id": "chat", "tag": "chat-1"}}
    assert message["data"] == {"type": "chat"}


def test_dead_tokens_are_reported_and_other_failures_are_not(monkeypatch):
    _patch_fcm(
        monkeypatch,
        {
            "gone": _response(404, {"error": {"status": "NOT_FOUND"}}),
            "unregistered": _response(
                400, {"error": {"details": [{"@type": "x", "errorCode": "UNREGISTERED"}], "message": "x"}}
            ),
            "server-hiccup": _response(500),
        },
    )
    dead = asyncio.run(PushService(SETTINGS).send_to_tokens(["ok", "gone", "unregistered", "server-hiccup"], _note()))
    assert sorted(dead) == ["gone", "unregistered"]


def test_network_errors_are_swallowed(monkeypatch):
    _patch_fcm(monkeypatch)

    async def boom(self, url, json, headers):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(push_service.httpx.AsyncClient, "post", boom)
    assert asyncio.run(PushService(SETTINGS).send_to_tokens(["a"], _note())) == []


def test_blog_push_goes_to_the_blog_topic_with_the_slug(monkeypatch):
    sent = _patch_fcm(monkeypatch)
    post = make_post(title="Online Booking", slug="online-booking", excerpt="Why it matters.")
    assert asyncio.run(PushService(SETTINGS).notify_blog_published(post)) is True
    (message,) = sent
    assert message["topic"] == "blog"
    assert message["data"] == {"type": "blog", "slug": "online-booking", "title": "Online Booking"}
    assert message["android"]["notification"]["channel_id"] == "blog"


# ---- When a blog post triggers a push ----


def test_first_publish_of_a_draft_pushes_once():
    post = make_post(is_published=False, published_at=None)
    service = make_blog_service([post])
    asyncio.run(service.update(post.id, is_published=True))
    assert service._push.blog_slugs == [post.slug]


def test_editing_a_live_post_does_not_push():
    post = make_post()
    service = make_blog_service([post])
    asyncio.run(service.update(post.id, excerpt="A sharper excerpt."))
    assert service._push.blog_slugs == []


def test_republishing_a_previously_live_post_does_not_push_again():
    post = make_post(is_published=False, published_at=NOW - timedelta(days=3))
    service = make_blog_service([post])
    asyncio.run(service.update(post.id, is_published=True))
    assert service._push.blog_slugs == []


def test_creating_a_published_post_pushes_but_a_draft_does_not():
    service = make_blog_service([])
    asyncio.run(service.create(**_post_fields("live-post"), is_published=True))
    asyncio.run(service.create(**_post_fields("draft-post"), is_published=False))
    assert service._push.blog_slugs == ["live-post"]


def test_auto_published_generated_post_pushes_and_held_draft_does_not():
    published = make_generator([], draft(), auto_publish=True)
    held = make_generator([], draft(), auto_publish=False)
    post = asyncio.run(published.generate_and_publish(trigger_source="test"))
    asyncio.run(held.generate_and_publish(trigger_source="test"))
    assert published._push.blog_slugs == [post.slug]
    assert held._push.blog_slugs == []


def _post_fields(slug: str) -> dict:
    return {
        "title": slug.replace("-", " ").title(),
        "slug": slug,
        "excerpt": "Excerpt.",
        "content": "word " * 50,
        "meta_title": f"Meta {slug}",
        "meta_description": f"Description for {slug}",
    }


# ---- Chat message push ----


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _chat_fixture(monkeypatch, conversation_type: str = "group"):
    sender, reader, left, blocked = (uuid.uuid4() for _ in range(4))

    def member(user_id, name, left_at=None, active=True):
        return SimpleNamespace(user_id=user_id, left_at=left_at, user=SimpleNamespace(full_name=name, is_active=active))

    conversation = SimpleNamespace(
        id=uuid.uuid4(),
        type=conversation_type,
        title="Launch team",
        participants=[
            member(sender, "Aditi"),
            member(reader, "Ravi"),
            member(left, "Former", left_at=NOW),
            member(blocked, "Blocked", active=False),
        ],
    )
    message = SimpleNamespace(id=uuid.uuid4(), body="Mockups are ready", attachments=None, is_deleted=False)
    asked_for: list[list[uuid.UUID]] = []
    deleted: list[list[str]] = []

    class FakeMessaging:
        def __init__(self, session):
            pass

        async def get_conversation(self, conversation_id):
            return conversation

        async def get_message(self, message_id):
            return message

    class FakeDevices:
        def __init__(self, session):
            pass

        async def tokens_for_users(self, user_ids):
            asked_for.append(list(user_ids))
            return ["reader-phone", "reader-old-phone"]

        async def delete_tokens(self, tokens):
            deleted.append(list(tokens))

    monkeypatch.setattr(push_service, "MessagingPersistence", FakeMessaging)
    monkeypatch.setattr(push_service, "DeviceTokenPersistence", FakeDevices)
    return SimpleNamespace(
        conversation=conversation,
        message=message,
        sender=sender,
        reader=reader,
        asked_for=asked_for,
        deleted=deleted,
        database=SimpleNamespace(session_scope=lambda: _Session()),
    )


def test_chat_message_pushes_only_to_other_active_members(monkeypatch):
    sent = _patch_fcm(monkeypatch, {"reader-old-phone": _response(404)})
    chat = _chat_fixture(monkeypatch)
    asyncio.run(notify_new_chat_message(chat.database, SETTINGS, chat.conversation.id, chat.message.id, chat.sender))

    assert chat.asked_for == [[chat.reader]]
    assert [m["token"] for m in sent] == ["reader-phone", "reader-old-phone"]
    assert sent[0]["notification"] == {"title": "Launch team", "body": "Aditi: Mockups are ready"}
    assert sent[0]["data"]["type"] == "chat"
    assert sent[0]["data"]["conversation_id"] == str(chat.conversation.id)
    assert chat.deleted == [["reader-old-phone"]]


def test_direct_message_is_titled_with_the_sender(monkeypatch):
    sent = _patch_fcm(monkeypatch)
    chat = _chat_fixture(monkeypatch, conversation_type="direct")
    asyncio.run(notify_new_chat_message(chat.database, SETTINGS, chat.conversation.id, chat.message.id, chat.sender))
    assert sent[0]["notification"] == {"title": "Aditi", "body": "Mockups are ready"}


def test_chat_push_never_raises(monkeypatch):
    _patch_fcm(monkeypatch)
    chat = _chat_fixture(monkeypatch)

    def broken_scope():
        raise RuntimeError("db down")

    chat.database.session_scope = broken_scope
    asyncio.run(notify_new_chat_message(chat.database, SETTINGS, chat.conversation.id, chat.message.id, chat.sender))
