"""Blog publishing pipeline: duplicate guard, deploy hook, public visibility.

No database or network: persistence, the LLM, email and the deploy hook are
replaced with in-memory fakes. Async code is driven with asyncio.run so the
suite needs nothing beyond pytest.
"""

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from core.exceptions import BadRequestError, ConflictError, NotFoundError
from schemas.content_schemas import AdminBlogPostResponse, BlogPostResponse
from services import deploy_hook_service
from services.blog_generation_service import (
    MIN_WORDS,
    SLUG_PATTERN,
    BlogGenerationError,
    BlogGenerationService,
    _closest_title,
    _normalize_headings,
    _truncate_to_word_limit,
    _word_count,
)
from services.blog_service import BlogService
from services.deploy_hook_service import DeployHookService

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def make_post(**overrides) -> SimpleNamespace:
    post = SimpleNamespace(
        id=uuid.uuid4(),
        title="Why Your Clinic Needs Online Booking",
        slug="why-your-clinic-needs-online-booking",
        excerpt="Old excerpt.",
        content=body("clinic", 300),
        cover_image_url=None,
        author_id=None,
        tags=["online booking"],
        is_published=True,
        published_at=NOW - timedelta(days=30),
        expires_at=None,
        meta_title="Online Booking for Clinics",
        meta_description="Why clinics should take bookings online instead of by phone, and how to get started.",
        keywords=["online booking system for clinics"],
        topic_tag="Clinic booking systems",
        word_count=300,
        created_at=NOW - timedelta(days=30),
        updated_at=NOW - timedelta(days=30),
    )
    for key, value in overrides.items():
        setattr(post, key, value)
    return post


def body(seed: str, words: int) -> str:
    """Markdown body of exactly `words` words whose vocabulary is unique to
    `seed`, so two bodies with different seeds share no phrases."""
    filler = " ".join(f"{seed}{i}" for i in range(words - 5))
    return f"{seed}hook {seed}start {seed}line.\n\n## {seed}section\n\n{filler}"


def draft(**overrides) -> dict:
    data = {
        "title": "Seven Signs Your Sales Team Has Outgrown Spreadsheets",
        "slug": "ignored",
        "excerpt": "How to tell when spreadsheets are holding your sales team back.",
        "content": body("crm", 900),
        "meta_title": "Signs You Have Outgrown Spreadsheets",
        "meta_description": "Seven practical signs your sales team needs a CRM instead of spreadsheets, and what to do next. Talk to Webnest Studio.",
        "keywords": ["custom crm for sales teams", "crm vs spreadsheets", "sales pipeline software"],
        "topic_tag": "Outgrowing spreadsheets",
    }
    data.update(overrides)
    return data


class FakeBlogs:
    def __init__(self, posts):
        self.posts = list(posts)
        self.created: list[dict] = []
        self.updated: list[tuple[SimpleNamespace, dict]] = []

    async def list_all(self):
        return list(self.posts)

    async def list_published(self, tag=None):
        return [p for p in self.posts if p.is_published]

    async def get_by_id(self, post_id):
        return next((p for p in self.posts if p.id == post_id), None)

    async def get_published_by_slug(self, slug):
        return next((p for p in self.posts if p.slug == slug and p.is_published), None)

    async def create(self, **fields):
        if any(p.slug == fields["slug"] for p in self.posts):
            raise ConflictError("A blog post with this slug already exists")
        self.created.append(fields)
        post = make_post(id=uuid.uuid4(), **fields)
        self.posts.append(post)
        return post

    async def update(self, post, **fields):
        self.updated.append((post, fields))
        for key, value in fields.items():
            setattr(post, key, value)
        post.updated_at = NOW
        return post

    async def delete(self, post):
        self.posts.remove(post)


class FakeHook:
    def __init__(self):
        self.reasons: list[str] = []

    async def trigger(self, reason: str) -> bool:
        self.reasons.append(reason)
        return True


class FakeLLM:
    def __init__(self, *drafts: dict):
        self._drafts = list(drafts)
        self.calls = 0

    async def generate_text(self, system_prompt: str, user_message: str):
        self.calls += 1
        current = self._drafts[min(self.calls - 1, len(self._drafts) - 1)]
        return json.dumps(current), "fake-llm"


class FakeLogs:
    def __init__(self):
        self.rows: list[dict] = []

    async def create(self, **fields):
        self.rows.append(fields)


class FakeEmail:
    def __init__(self):
        self.sent: list[str] = []

    async def send_blog_published_notification(self, title, slug):
        self.sent.append(slug)


def make_generator(posts, *drafts):
    service = BlogGenerationService.__new__(BlogGenerationService)
    service._blog_posts = FakeBlogs(posts)
    service._logs = FakeLogs()
    service._settings = SimpleNamespace(blog_generation_interval_days=7, team_notification_email="")
    service._llm = FakeLLM(*drafts)
    service._email = FakeEmail()
    service._deploy_hook = FakeHook()
    service.last_outcome_detail = ""
    return service


def make_blog_service(posts):
    service = BlogService.__new__(BlogService)
    service._blog_posts = FakeBlogs(posts)
    service._settings = None
    service._deploy_hook = FakeHook()
    return service


# ---- Generator: duplicate-topic guard ----


def test_new_topic_creates_a_permanent_post_and_triggers_the_deploy_hook():
    existing = make_post()
    service = make_generator([existing], draft())

    post = asyncio.run(service.generate_and_publish("manual-admin"))

    assert len(service._blog_posts.created) == 1
    assert service._blog_posts.updated == []
    assert post.slug == "seven-signs-your-sales-team-has-outgrown-spreadsheets"
    assert SLUG_PATTERN.match(post.slug)
    assert post.expires_at is None
    assert post.word_count >= MIN_WORDS
    assert service._deploy_hook.reasons == [f"blog post created: {post.slug}"]
    assert service._email.sent == [post.slug]


@pytest.mark.parametrize(
    "overrides",
    [
        {"topic_tag": "clinic booking systems"},  # same topic_tag
        {"title": "Why Your Clinic Needs Online Booking Now"},  # near-identical title
        # same primary keyword on an overlapping (but not near-identical) title
        {
            "title": "Online Booking Mistakes Every Dental Clinic Makes",
            "keywords": ["Online Booking System for Clinics", "something else", "third"],
        },
    ],
)
def test_matching_topic_updates_the_existing_post_instead_of_adding_a_slug(overrides):
    existing = make_post()
    original_slug = existing.slug
    # The model keeps returning the same duplicate, so every retry matches too.
    service = make_generator([existing], draft(**overrides))

    post = asyncio.run(service.generate_and_publish("scheduled-recurring"))

    assert post is existing
    assert service._blog_posts.created == []
    assert len(service._blog_posts.posts) == 1
    assert post.slug == original_slug
    assert post.word_count >= MIN_WORDS
    assert post.updated_at == NOW
    assert "slug" not in service._blog_posts.updated[0][1]
    assert service._deploy_hook.reasons == [f"blog post refreshed: {original_slug}"]
    assert "updated instead" in service.last_outcome_detail


def test_identical_title_never_creates_a_numeric_suffix_slug():
    existing = make_post()
    service = make_generator([existing], draft(title=existing.title, topic_tag="A different label"))

    post = asyncio.run(service.generate_and_publish("manual-admin", topic_hint="clinic booking"))

    assert [p.slug for p in service._blog_posts.posts] == [existing.slug]
    assert post.slug == existing.slug


def test_retry_that_finds_a_fresh_topic_creates_a_new_post():
    existing = make_post()
    service = make_generator([existing], draft(topic_tag="Clinic booking systems"), draft())

    post = asyncio.run(service.generate_and_publish("scheduled-recurring"))

    assert len(service._blog_posts.created) == 1
    assert post is not existing
    assert service._blog_posts.updated == []


def test_shared_primary_keyword_alone_does_not_overwrite_an_unrelated_post():
    existing = make_post()
    before = existing.content
    service = make_generator([existing], draft(keywords=["Online Booking System for Clinics", "other", "third"]))

    post = asyncio.run(service.generate_and_publish("scheduled-recurring"))

    assert post is not existing
    assert existing.content == before
    assert len(service._blog_posts.created) == 1


def test_shorter_draft_does_not_overwrite_a_longer_post_or_use_up_the_weekly_slot():
    existing = make_post(content=body("clinic", 1100), word_count=1100)
    service = make_generator([existing], draft(topic_tag="Clinic booking systems"))

    with pytest.raises(BlogGenerationError, match="Nothing was changed"):
        asyncio.run(service.generate_and_publish("scheduled-recurring"))

    assert service._blog_posts.updated == [] and service._blog_posts.created == []
    assert service._deploy_hook.reasons == []
    # Logged as a failure, so should_run_recurring() tries again at the next cron fire.
    assert [row["success"] for row in service._logs.rows] == [False]


def test_generated_meta_fields_are_made_unique():
    existing = make_post()
    service = make_generator(
        [existing],
        draft(meta_title=existing.meta_title, meta_description=existing.meta_description),
    )

    post = asyncio.run(service.generate_and_publish("manual-admin"))

    assert post is not existing
    assert post.meta_title.lower() != existing.meta_title.lower()
    assert post.meta_description.lower() != existing.meta_description.lower()


@pytest.mark.parametrize("overrides", [{"topic_tag": "Clinic booking systems"}, {"title": "Why Your Clinic Needs Online Booking"}])
def test_draft_matching_an_unpublished_post_never_republishes_or_shadows_it(overrides):
    expired = make_post(is_published=False, expires_at=NOW - timedelta(days=60))
    before = (expired.content, expired.is_published, expired.expires_at)
    service = make_generator([expired], draft(**overrides))

    with pytest.raises(BlogGenerationError, match="unpublished post"):
        asyncio.run(service.generate_and_publish("scheduled-recurring"))

    assert (expired.content, expired.is_published, expired.expires_at) == before
    assert service._blog_posts.updated == [] and service._blog_posts.created == []
    assert service._deploy_hook.reasons == []
    assert [row["success"] for row in service._logs.rows] == [False]


def test_unexpected_error_is_reported_as_a_logged_generation_failure():
    service = make_generator([], draft())

    async def broken_list_all():
        raise RuntimeError("database exploded")

    service._blog_posts.list_all = broken_list_all

    with pytest.raises(BlogGenerationError, match="Unexpected error: RuntimeError"):
        asyncio.run(service.generate_and_publish("scheduled-recurring"))
    assert [row["success"] for row in service._logs.rows] == [False]


def test_recurring_run_waits_a_week_after_the_last_successful_run():
    service = make_generator([], draft())
    service._blog_posts.get_latest = lambda: _async(None)

    async def run(days_ago: float) -> bool:
        async def latest_success_at():
            return datetime.now(timezone.utc) - timedelta(days=days_ago)

        service._logs.latest_success_at = latest_success_at
        return await service.should_run_recurring()

    assert asyncio.run(run(3)) is False
    # The cron fires at the same clock time each day, a few minutes "early"
    # relative to when the previous run finished.
    assert asyncio.run(run(6.99)) is True


async def _async(value):
    return value


# ---- Deploy hook ----


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _patch_httpx(monkeypatch, *, status_code: int = 201, error: Exception | None = None) -> list[str]:
    calls: list[str] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url):
            calls.append(url)
            if error is not None:
                raise error
            return _FakeResponse(status_code)

    monkeypatch.setattr(deploy_hook_service.httpx, "AsyncClient", FakeClient)
    return calls


def _hook(url: str) -> DeployHookService:
    return DeployHookService(SimpleNamespace(vercel_deploy_hook_url=url, vercel_deploy_hook_timeout_seconds=5.0))


def test_deploy_hook_posts_to_the_configured_url(monkeypatch):
    calls = _patch_httpx(monkeypatch)
    assert asyncio.run(_hook("https://api.vercel.com/v1/integrations/deploy/abc").trigger("test")) is True
    assert calls == ["https://api.vercel.com/v1/integrations/deploy/abc"]


@pytest.mark.parametrize(
    "configured",
    [
        '"https://api.vercel.com/v1/integrations/deploy/abc/xyz"',
        "'https://api.vercel.com/v1/integrations/deploy/abc/xyz'",
        "  https://api.vercel.com/v1/integrations/deploy/abc/xyz  ",
        "curl -X POST https://api.vercel.com/v1/integrations/deploy/abc/xyz",
        'curl -X POST "https://api.vercel.com/v1/integrations/deploy/abc/xyz"',
    ],
)
def test_deploy_hook_tolerates_quotes_and_a_pasted_curl_command(monkeypatch, configured):
    calls = _patch_httpx(monkeypatch)
    assert asyncio.run(_hook(configured).trigger("test")) is True
    assert calls == ["https://api.vercel.com/v1/integrations/deploy/abc/xyz"]


@pytest.mark.parametrize("configured", ["not a url", '""', None])
def test_deploy_hook_with_no_url_in_the_value_is_skipped(monkeypatch, configured):
    calls = _patch_httpx(monkeypatch)
    assert asyncio.run(_hook(configured).trigger("test")) is False
    assert calls == []


def test_deploy_hook_is_skipped_when_not_configured(monkeypatch):
    calls = _patch_httpx(monkeypatch)
    assert asyncio.run(_hook("").trigger("test")) is False
    assert calls == []


@pytest.mark.parametrize("kwargs", [{"status_code": 500}, {"error": ConnectionError("network down")}])
def test_deploy_hook_failure_is_swallowed(monkeypatch, kwargs):
    _patch_httpx(monkeypatch, **kwargs)
    assert asyncio.run(_hook("https://example.invalid/hook").trigger("test")) is False


def test_publishing_still_succeeds_when_the_deploy_hook_fails(monkeypatch):
    _patch_httpx(monkeypatch, error=ConnectionError("network down"))
    service = make_generator([], draft())
    service._deploy_hook = _hook("https://example.invalid/hook")

    post = asyncio.run(service.generate_and_publish("manual-admin"))

    assert post.is_published is True
    assert service._logs.rows[0]["success"] is True


# ---- Admin CRUD (BlogService) ----


def test_admin_publish_update_unpublish_and_delete_trigger_the_deploy_hook():
    service = make_blog_service([])

    post = asyncio.run(
        service.create(title="Hello", slug="hello", content="one two three", is_published=True, meta_title="Hello")
    )
    assert post.expires_at is None  # no automatic expiry
    assert post.published_at is not None
    assert post.word_count == 3

    asyncio.run(service.update(post.id, content="one two three four"))
    asyncio.run(service.update(post.id, is_published=False))
    asyncio.run(service.update(post.id, excerpt="still a draft"))  # unpublished edit: no rebuild
    asyncio.run(service.update(post.id, is_published=True))
    asyncio.run(service.delete(post.id))

    assert service._deploy_hook.reasons == [
        "post published: hello",
        "post updated: hello",
        "post unpublished: hello",
        "post updated: hello",
        "post deleted: hello",
    ]


def test_admin_draft_does_not_trigger_the_deploy_hook():
    service = make_blog_service([])
    asyncio.run(service.create(title="Draft", slug="draft", content="x", is_published=False))
    assert service._deploy_hook.reasons == []


def test_republishing_clears_a_stale_expiry():
    expired = make_post(is_published=False, expires_at=datetime.now(timezone.utc) - timedelta(days=40))
    service = make_blog_service([expired])
    post = asyncio.run(service.update(expired.id, is_published=True))
    assert post.expires_at is None


def test_slug_of_a_published_post_cannot_be_changed():
    existing = make_post()
    service = make_blog_service([existing])
    with pytest.raises(BadRequestError):
        asyncio.run(service.update(existing.id, slug="a-new-slug"))
    asyncio.run(service.update(existing.id, slug=existing.slug))  # unchanged slug is fine
    # A form that sends "slug": null means "leave it alone", not "clear it".
    post = asyncio.run(service.update(existing.id, slug=None, excerpt="edited"))
    assert post.slug == existing.slug and post.excerpt == "edited"
    assert "slug" not in service._blog_posts.updated[-1][1]


def test_null_slug_on_a_draft_keeps_its_slug():
    draft_post = make_post(is_published=False, published_at=None)
    service = make_blog_service([draft_post])
    post = asyncio.run(service.update(draft_post.id, slug=None))
    assert post.slug == "why-your-clinic-needs-online-booking"


def test_duplicate_meta_title_is_rejected_for_published_posts():
    existing = make_post()
    service = make_blog_service([existing])
    with pytest.raises(ConflictError):
        asyncio.run(
            service.create(title="Other", slug="other", content="x", is_published=True, meta_title=existing.meta_title.upper())
        )


# ---- Public visibility ----


def test_public_slug_lookup_is_404_for_unpublished_posts_and_200_for_published():
    live = make_post(slug="live")
    hidden = make_post(slug="hidden", is_published=False)
    service = make_blog_service([live, hidden])

    assert asyncio.run(service.get_published_by_slug("live")) is live
    with pytest.raises(NotFoundError):
        asyncio.run(service.get_published_by_slug("hidden"))
    with pytest.raises(NotFoundError):
        asyncio.run(service.get_published_by_slug("missing"))


def test_responses_include_updated_at_and_admin_gets_a_short_post_warning():
    short = make_post(word_count=254)
    assert BlogPostResponse.model_validate(short).updated_at == short.updated_at
    assert "warnings" not in BlogPostResponse.model_validate(short).model_dump()
    assert "254 words" in AdminBlogPostResponse.model_validate(short).warnings[0]
    assert AdminBlogPostResponse.model_validate(make_post(word_count=950)).warnings == []


# ---- Text helpers ----


def test_title_similarity():
    existing = ["React Frontend with Python and Java Backend Development for Scaling Businesses"]
    assert _closest_title("React Frontend with Python and Java Backend: The Ultimate Tech Stack for Growing Businesses", existing)
    assert _closest_title("Seven Signs Your Sales Team Has Outgrown Spreadsheets", existing) is None


def test_heading_normalisation_keeps_valid_headings_intact():
    content = "Intro.\n\n## Section\n\nText. ## Glued heading\n\n### A question?\n\nAnswer."
    assert _normalize_headings(content) == (
        "Intro.\n\n## Section\n\nText. \n\n## Glued heading\n\n### A question?\n\nAnswer."
    )


def test_truncation_keeps_markdown_structure():
    content = "\n\n".join(["Intro " + "a " * 50, "## One", "b " * 50, "## Two", "c " * 50])
    result = _truncate_to_word_limit(content, 110)
    assert _word_count(result) <= 110
    assert "## One" in result and "\n\n" in result
    assert not result.rstrip().endswith("## Two")
