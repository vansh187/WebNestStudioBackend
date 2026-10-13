import uuid
from collections.abc import Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert

from database.base_persistence import BasePersistence
from database.models import DeviceToken


class DeviceTokenPersistence(BasePersistence):
    """CRUD access to the device_tokens table (FCM registration tokens)."""

    async def upsert(self, user_id: uuid.UUID, token: str, platform: str) -> None:
        """Registers token for user_id. A token already held by someone else
        (another account signed in on this phone before) moves to user_id."""
        statement = (
            insert(DeviceToken)
            .values(user_id=user_id, token=token, platform=platform)
            .on_conflict_do_update(
                index_elements=[DeviceToken.token],
                set_={"user_id": user_id, "platform": platform, "last_seen_at": func.now()},
            )
        )
        await self._execute(statement)
        await self._commit()

    async def delete_for_user(self, user_id: uuid.UUID, token: str) -> None:
        await self._execute(
            delete(DeviceToken).where(DeviceToken.user_id == user_id, DeviceToken.token == token)
        )
        await self._commit()

    async def delete_tokens(self, tokens: Sequence[str]) -> None:
        if not tokens:
            return
        await self._execute(delete(DeviceToken).where(DeviceToken.token.in_(list(tokens))))
        await self._commit()

    async def tokens_for_users(self, user_ids: Sequence[uuid.UUID]) -> list[str]:
        if not user_ids:
            return []
        result = await self._execute(select(DeviceToken.token).where(DeviceToken.user_id.in_(list(user_ids))))
        return list(result.scalars().all())
