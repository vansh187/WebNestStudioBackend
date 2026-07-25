import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.base_persistence import BasePersistence
from database.models import User


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
