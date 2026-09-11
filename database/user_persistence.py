import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import delete, or_, select
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.base_persistence import BasePersistence
from database.models import CodingExecution, CodingShare, Lead, NewsletterSubscriber, OtpVerification, User


class UserPersistence(BasePersistence):
    """CRUD access to the users table."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def create(self, full_name: str | None, email: str, phone_number: str | None, password_hash: str) -> User:
        user = User(full_name=full_name, email=email, phone_number=phone_number, password_hash=password_hash)
        self._session.add(user)
        await self._commit(conflict_message="An account with this email already exists")
        await self._refresh(user)
        return user

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        result = await self._execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> User | None:
        result = await self._execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()

    async def get_by_email_ci(self, email: str) -> User | None:
        """Case-insensitive email lookup — for flows where the caller (e.g. an
        admin typing someone else's address) can't be relied on to know the
        exact casing the account was created with."""
        result = await self._execute(
            select(User).where(func.lower(User.email) == email.strip().lower())
        )
        return result.scalar_one_or_none()

    async def mark_verified(self, user: User) -> User:
        user.is_verified = True
        await self._commit()
        await self._refresh(user)
        return user

    async def update_password_hash(self, user: User, password_hash: str) -> User:
        user.password_hash = password_hash
        await self._commit()
        await self._refresh(user)
        return user

    async def update_last_login(self, user: User, when: datetime) -> User:
        user.last_login_at = when
        await self._commit()
        await self._refresh(user)
        return user

    async def set_active(self, user: User, is_active: bool) -> User:
        user.is_active = is_active
        await self._commit()
        await self._refresh(user)
        return user

    async def delete_account(self, user: User) -> None:
        """Delete the account and directly email-linked records.

        Most user-owned feature data is removed by ON DELETE CASCADE from the
        users row. These extra deletes cover tables that intentionally use
        SET NULL or store the email address independently.
        """
        await self._execute(
            delete(OtpVerification).where(
                or_(OtpVerification.user_id == user.id, OtpVerification.email == user.email)
            )
        )
        await self._execute(
            delete(Lead).where(or_(Lead.user_id == user.id, Lead.email == user.email))
        )
        await self._execute(delete(NewsletterSubscriber).where(NewsletterSubscriber.email == user.email))
        await self._execute(delete(CodingShare).where(CodingShare.owner_user_id == user.id))
        await self._execute(delete(CodingExecution).where(CodingExecution.user_id == user.id))
        await self._delete(user)

    async def search(self, query: str, exclude_user_id: uuid.UUID, limit: int = 20) -> list[User]:
        """Case-insensitive ILIKE match on full_name OR email, active accounts
        only, never the caller. Used by the messaging feature's people picker -
        there is no way to add a non-registered person to a chat."""
        limit = min(max(limit, 1), 50)
        term = query.strip()
        if len(term) < 2:
            return []
        # Escape LIKE metacharacters so a user typing "%" or "_" searches for
        # those literal characters instead of turning the pattern into a wildcard.
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        statement = (
            select(User)
            .where(
                User.id != exclude_user_id,
                User.is_active.is_(True),
                or_(
                    User.full_name.ilike(pattern, escape="\\"),
                    User.email.ilike(pattern, escape="\\"),
                ),
            )
            .order_by(User.full_name.asc().nullslast(), User.email.asc())
            .limit(limit)
        )
        result = await self._execute(statement)
        return list(result.scalars().all())

    async def get_active_by_ids(self, user_ids: Sequence[uuid.UUID]) -> list[User]:
        """Returns the subset of user_ids that exist and are active. Callers
        compare the returned count/ids against what they asked for to reject
        conversations that reference unknown users."""
        unique_ids = list({user_id for user_id in user_ids})
        if not unique_ids:
            return []
        result = await self._execute(
            select(User).where(User.id.in_(unique_ids), User.is_active.is_(True))
        )
        return list(result.scalars().all())
