from sqlalchemy.ext.asyncio import AsyncSession

from database.models import NewsletterSubscriber
from database.newsletter_persistence import NewsletterPersistence


class NewsletterService:
    """Handles newsletter subscribe / resubscribe flow."""

    def __init__(self, session: AsyncSession) -> None:
        self._subscribers = NewsletterPersistence(session)

    async def subscribe(self, email: str, source: str | None) -> NewsletterSubscriber:
        existing = await self._subscribers.get_by_email(email)
        if existing is None:
            return await self._subscribers.create(email, source)
        if not existing.subscribed:
            return await self._subscribers.resubscribe(existing)
        return existing
