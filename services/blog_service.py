import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import NotFoundError
from database.blog_persistence import BlogPersistence
from database.models import BlogPost


class BlogService:
    """Business logic for blog posts."""

    def __init__(self, session: AsyncSession) -> None:
        self._blog_posts = BlogPersistence(session)

    async def list_published(self, tag: str | None = None) -> list[BlogPost]:
        return await self._blog_posts.list_published(tag=tag)

    async def list_all(self) -> list[BlogPost]:
        return await self._blog_posts.list_all()

    async def get_by_slug(self, slug: str) -> BlogPost:
        post = await self._blog_posts.get_by_slug(slug)
        if post is None:
            raise NotFoundError("Blog post not found")
        return post

    async def create(self, **fields) -> BlogPost:
        return await self._blog_posts.create(**fields)

    async def update(self, post_id: uuid.UUID, **fields) -> BlogPost:
        post = await self._blog_posts.get_by_id(post_id)
        if post is None:
            raise NotFoundError("Blog post not found")
        return await self._blog_posts.update(post, **fields)

    async def delete(self, post_id: uuid.UUID) -> None:
        post = await self._blog_posts.get_by_id(post_id)
        if post is None:
            raise NotFoundError("Blog post not found")
        await self._blog_posts.delete(post)
