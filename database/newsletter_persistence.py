from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.base_persistence import BasePersistence
from database.models import NewsletterSubscriber


class NewsletterPersistence(BasePersistence):
    """CRUD access to the newsletter_subscribers table."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def get_by_email(self, email: str) -> NewsletterSubscriber | None:
        result = await self._execute(select(NewsletterSubscriber).where(NewsletterSubscriber.email == email))
        return result.scalar_one_or_none()

    async def create(self, email: str, source: str | None) -> NewsletterSubscriber:
        subscriber = NewsletterSubscriber(email=email, source=source)
        self._session.add(subscriber)
        await self._commit(conflict_message="This email is already subscribed to the newsletter")
        await self._refresh(subscriber)
        return subscriber

    async def resubscribe(self, subscriber: NewsletterSubscriber) -> NewsletterSubscriber:
        subscriber.subscribed = True
        await self._commit()
        await self._refresh(subscriber)
        return subscriber
