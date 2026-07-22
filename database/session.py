from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from core.config import Settings


class Base(DeclarativeBase):
    """Shared declarative base for every ORM model in the database package."""


class Database:
    """Owns the async engine/session factory for the configured Postgres instance."""

    def __init__(self, settings: Settings) -> None:
        self._engine: AsyncEngine = create_async_engine(settings.async_database_url, pool_pre_ping=True)
        self._session_factory = async_sessionmaker(
            bind=self._engine, class_=AsyncSession, expire_on_commit=False
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    async def create_all(self) -> None:
        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self._engine.dispose()

    async def get_session(self) -> AsyncGenerator[AsyncSession, None]:
        async with self._session_factory() as session:
            yield session
