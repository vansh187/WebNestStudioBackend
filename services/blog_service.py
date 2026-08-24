import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings
from core.exceptions import NotFoundError
from database.blog_persistence import BlogPersistence
from database.models import BlogPost


class BlogService:
    """Business logic for blog posts."""

    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self._blog_posts = BlogPersistence(session)
        self._settings = settings

    async def list_published(self, tag: str | None = None) -> list[BlogPost]:
        return await self._blog_posts.list_published(tag=tag)

    async def get_latest(self) -> BlogPost | None:
        return await self._blog_posts.get_latest()

    async def list_topic_tags(self) -> list[str]:
        return await self._blog_posts.list_topic_tags()

    async def has_published_on_date(self, day_start: datetime, day_end: datetime) -> bool:
        return await self._blog_posts.has_published_on_date(day_start, day_end)

    async def archive_expired(self) -> int:
        return await self._blog_posts.archive_expired()

    async def list_all(self) -> list[BlogPost]:
        return await self._blog_posts.list_all()

    async def get_by_slug(self, slug: str) -> BlogPost:
        post = await self._blog_posts.get_by_slug(slug)
        if post is None:
            raise NotFoundError("Blog post not found")
        return post

    async def create(self, **fields) -> BlogPost:
        # There are no permanent posts in this system - every post (including
        # ones created manually through this admin endpoint) expires 15 days
        # after its own publish date unless the caller explicitly overrides
        # expires_at. Without this default, a manually-created post with no
        # expires_at would be treated as "never expires" by list_published().
        if fields.get("expires_at") is None and self._settings is not None:
            anchor = fields.get("published_at") or datetime.now(timezone.utc)
            fields["expires_at"] = anchor + timedelta(days=self._settings.blog_post_lifetime_days)
        return await self._blog_posts.create(**fields)

    async def update(self, post_id: uuid.UUID, **fields) -> BlogPost:
        post = await self._blog_posts.get_by_id(post_id)
        if post is None:
            raise NotFoundError("Blog post not found")

        if self._settings is not None:
            if "expires_at" in fields and fields["expires_at"] is None:
                # There are no permanent posts in this system - an explicit
                # null is treated as "reset to the standard window" rather
                # than "never expires", so this can't be used to pin a post
                # forever.
                anchor = fields.get("published_at") or post.published_at or datetime.now(timezone.utc)
                fields["expires_at"] = anchor + timedelta(days=self._settings.blog_post_lifetime_days)
            elif "expires_at" not in fields:
                # Mirror create()'s auto-expiry when the caller didn't touch
                # expires_at at all. Without this, a draft published (or
                # re-dated) well after its original creation would keep the
                # stale expires_at from creation time and could already be in
                # the past the instant it's published - invisible immediately
                # and flipped back to unpublished by the next archive sweep.
                is_publishing = fields.get("is_published") is True and not post.is_published
                published_at_changed = fields.get("published_at") is not None
                if is_publishing or published_at_changed:
                    anchor = fields.get("published_at") or post.published_at or datetime.now(timezone.utc)
                    fields["expires_at"] = anchor + timedelta(days=self._settings.blog_post_lifetime_days)

        return await self._blog_posts.update(post, **fields)

    async def delete(self, post_id: uuid.UUID) -> None:
        post = await self._blog_posts.get_by_id(post_id)
        if post is None:
            raise NotFoundError("Blog post not found")
        await self._blog_posts.delete(post)
