import uuid
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.base_persistence import BasePersistence
from database.models import BlogPost


class BlogPersistence(BasePersistence):
    """CRUD access to the blog_posts table."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def create(self, **fields) -> BlogPost:
        post = BlogPost(**fields)
        self._session.add(post)
        await self._commit(conflict_message="A blog post with this slug already exists")
        await self._refresh(post)
        return post

    async def get_by_id(self, post_id: uuid.UUID) -> BlogPost | None:
        result = await self._execute(select(BlogPost).where(BlogPost.id == post_id))
        return result.scalar_one_or_none()

    async def get_by_slug(self, slug: str) -> BlogPost | None:
        result = await self._execute(select(BlogPost).where(BlogPost.slug == slug))
        return result.scalar_one_or_none()

    async def list_published(self, tag: str | None = None) -> list[BlogPost]:
        # expires_at is enforced here (not only by the daily archive sweep) so a
        # post never stays publicly visible past its 15-day window even if the
        # sweep job is delayed or hasn't run yet since expiry.
        query = select(BlogPost).where(
            BlogPost.is_published.is_(True),
            or_(BlogPost.expires_at.is_(None), BlogPost.expires_at > func.now()),
        )
        if tag:
            query = query.where(BlogPost.tags.any(tag))
        query = query.order_by(BlogPost.published_at.desc())
        result = await self._execute(query)
        return list(result.scalars().all())

    async def list_all(self) -> list[BlogPost]:
        result = await self._execute(select(BlogPost).order_by(BlogPost.created_at.desc()))
        return list(result.scalars().all())

    async def get_latest(self) -> BlogPost | None:
        """Most recently published post (any status), used to gate the
        recurring-generation 2-day cooldown."""
        query = select(BlogPost).where(BlogPost.published_at.is_not(None)).order_by(BlogPost.published_at.desc()).limit(1)
        result = await self._execute(query)
        return result.scalar_one_or_none()

    async def list_topic_tags(self) -> list[str]:
        """Every historical topic_tag ever used (any status), so a topic is
        never picked twice across the site's lifetime."""
        query = select(BlogPost.topic_tag).where(BlogPost.topic_tag.is_not(None)).order_by(BlogPost.created_at.desc())
        result = await self._execute(query)
        return [row[0] for row in result.all() if row[0]]

    async def has_published_on_date(self, day_start: datetime, day_end: datetime) -> bool:
        """Whether any post's published_at falls within [day_start, day_end) -
        used to make the one-off launch post idempotent across restarts."""
        query = select(BlogPost.id).where(
            BlogPost.published_at.is_not(None),
            BlogPost.published_at >= day_start,
            BlogPost.published_at < day_end,
        ).limit(1)
        result = await self._execute(query)
        return result.scalar_one_or_none() is not None

    async def archive_expired(self) -> int:
        """Flips is_published=False for posts past their expiry. Housekeeping
        only - list_published() already filters expiry in real time - but this
        keeps the admin list and DB state tidy."""
        query = select(BlogPost).where(
            BlogPost.is_published.is_(True),
            BlogPost.expires_at.is_not(None),
            BlogPost.expires_at <= func.now(),
        )
        result = await self._execute(query)
        expired = list(result.scalars().all())
        for post in expired:
            post.is_published = False
        if expired:
            await self._commit()
        return len(expired)

    async def update(self, post: BlogPost, **fields) -> BlogPost:
        # Every caller passes fields already filtered to "explicitly provided
        # by this request" (BlogService.update() uses model_dump(exclude_unset=True)),
        # so a key being present - even with value None - means the caller
        # deliberately wants that field cleared (e.g. expires_at=null to
        # un-expire a post, or meta_title=null to clear stale SEO copy).
        # Silently dropping None here would make those fields un-clearable.
        for key, value in fields.items():
            setattr(post, key, value)
        await self._commit(conflict_message="A blog post with this slug already exists")
        await self._refresh(post)
        return post

    async def delete(self, post: BlogPost) -> None:
        await self._delete(post)
