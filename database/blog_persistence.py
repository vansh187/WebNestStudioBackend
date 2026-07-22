import uuid

from sqlalchemy import select
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
        query = select(BlogPost).where(BlogPost.is_published.is_(True))
        if tag:
            query = query.where(BlogPost.tags.any(tag))
        query = query.order_by(BlogPost.published_at.desc())
        result = await self._execute(query)
        return list(result.scalars().all())

    async def list_all(self) -> list[BlogPost]:
        result = await self._execute(select(BlogPost).order_by(BlogPost.created_at.desc()))
        return list(result.scalars().all())

    async def update(self, post: BlogPost, **fields) -> BlogPost:
        for key, value in fields.items():
            if value is not None:
                setattr(post, key, value)
        await self._commit(conflict_message="A blog post with this slug already exists")
        await self._refresh(post)
        return post

    async def delete(self, post: BlogPost) -> None:
        await self._delete(post)
