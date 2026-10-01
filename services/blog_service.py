import re
import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings
from core.exceptions import BadRequestError, ConflictError, NotFoundError
from database.blog_persistence import BlogPersistence
from database.models import BlogPost
from services.deploy_hook_service import DeployHookService

_WORD_PATTERN = re.compile(r"\S+")


def count_words(text: str | None) -> int:
    return len(_WORD_PATTERN.findall(text or ""))


def _normalized(value: str | None) -> str:
    return " ".join((value or "").lower().split())


class BlogService:
    """Business logic for blog posts."""

    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self._blog_posts = BlogPersistence(session)
        self._settings = settings
        self._deploy_hook = DeployHookService(settings)

    async def list_published(self, tag: str | None = None) -> list[BlogPost]:
        return await self._blog_posts.list_published(tag=tag)

    async def get_latest(self) -> BlogPost | None:
        return await self._blog_posts.get_latest()

    async def list_topic_tags(self) -> list[str]:
        return await self._blog_posts.list_topic_tags()

    async def has_published_on_date(self, day_start: datetime, day_end: datetime) -> bool:
        return await self._blog_posts.has_published_on_date(day_start, day_end)

    async def archive_expired(self) -> int:
        archived = await self._blog_posts.archive_expired()
        if archived:
            await self._deploy_hook.trigger(f"{archived} post(s) reached their manual expiry")
        return archived

    async def list_all(self) -> list[BlogPost]:
        return await self._blog_posts.list_all()

    async def get_published_by_slug(self, slug: str) -> BlogPost:
        """Public lookup: unpublished and expired posts are a 404, exactly as
        they are absent from list_published()."""
        post = await self._blog_posts.get_published_by_slug(slug)
        if post is None:
            raise NotFoundError("Blog post not found")
        return post

    async def create(self, **fields) -> BlogPost:
        # Posts are permanent: expires_at stays null unless the caller sets it.
        if fields.get("is_published") and fields.get("published_at") is None:
            fields["published_at"] = datetime.now(timezone.utc)
        fields["word_count"] = count_words(fields.get("content"))
        if fields.get("is_published"):
            await self._ensure_unique_seo_fields(fields.get("meta_title"), fields.get("meta_description"), exclude_id=None)
        post = await self._blog_posts.create(**fields)
        if post.is_published:
            await self._deploy_hook.trigger(f"post published: {post.slug}")
        return post

    async def update(self, post_id: uuid.UUID, **fields) -> BlogPost:
        post = await self._blog_posts.get_by_id(post_id)
        if post is None:
            raise NotFoundError("Blog post not found")

        if fields.get("slug") is None:
            # An admin form that sends "slug": null means "leave it alone" -
            # a post can never be without a slug.
            fields.pop("slug", None)
        if "slug" in fields and fields["slug"] != post.slug and post.published_at is not None:
            # The URL of a post that has ever been live may be indexed -
            # changing it would turn that indexed URL into a 404.
            raise BadRequestError("The slug of a post that has been published cannot be changed")

        was_published = post.is_published
        is_publishing = fields.get("is_published") is True and not was_published
        if is_publishing:
            if fields.get("published_at") is None and post.published_at is None:
                fields["published_at"] = datetime.now(timezone.utc)
            if "expires_at" not in fields and post.expires_at is not None and _as_aware(post.expires_at) <= datetime.now(timezone.utc):
                # A stale expiry from the old 15-day lifetime would hide the
                # post again the moment it is republished.
                fields["expires_at"] = None
        if "content" in fields:
            fields["word_count"] = count_words(fields["content"])

        if fields.get("is_published", was_published):
            await self._ensure_unique_seo_fields(
                fields.get("meta_title", post.meta_title),
                fields.get("meta_description", post.meta_description),
                exclude_id=post.id,
            )
        post = await self._blog_posts.update(post, **fields)
        if was_published or post.is_published:
            action = "unpublished" if was_published and not post.is_published else "updated"
            await self._deploy_hook.trigger(f"post {action}: {post.slug}")
        return post

    async def delete(self, post_id: uuid.UUID) -> None:
        post = await self._blog_posts.get_by_id(post_id)
        if post is None:
            raise NotFoundError("Blog post not found")
        was_published, slug = post.is_published, post.slug
        await self._blog_posts.delete(post)
        if was_published:
            await self._deploy_hook.trigger(f"post deleted: {slug}")

    async def _ensure_unique_seo_fields(
        self, meta_title: str | None, meta_description: str | None, exclude_id: uuid.UUID | None
    ) -> None:
        """A live post's meta_title and meta_description must not repeat
        another live post's - two pages with the same search snippet compete
        with each other. Drafts and unpublished posts are not checked."""
        wanted = {"meta_title": _normalized(meta_title), "meta_description": _normalized(meta_description)}
        wanted = {key: value for key, value in wanted.items() if value}
        if not wanted:
            return
        for other in await self._blog_posts.list_published():
            if other.id == exclude_id:
                continue
            for key, value in wanted.items():
                if _normalized(getattr(other, key)) == value:
                    raise ConflictError(f"Another published blog post ({other.slug}) already uses this {key}")


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
