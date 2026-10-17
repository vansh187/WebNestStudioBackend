import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError

from core.exceptions import DatabaseError
from database.base_persistence import BasePersistence, logger
from database.models import GitHubConnection, GitHubOAuthState

# Concurrent OAuth flows one user may have open at once.
MAX_LIVE_STATES = 5


class GitHubPersistence(BasePersistence):
    """DB access for CodeLab GitHub sync: the per-user connection (encrypted
    token) and single-use OAuth states."""

    # ------------------------------------------------------------------ #
    # Connection
    # ------------------------------------------------------------------ #
    async def get_connection(self, user_id: uuid.UUID) -> GitHubConnection | None:
        # populate_existing: the row is also written with core INSERT/UPSERT
        # statements that bypass the identity map, so a copy loaded earlier
        # in this session must be refreshed, never returned stale.
        result = await self._execute(
            select(GitHubConnection)
            .where(GitHubConnection.user_id == user_id)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def other_users_linked_to(self, github_user_id: int, user_id: uuid.UUID) -> bool:
        """Whether another WebNest user has linked the same GitHub account."""
        result = await self._execute(
            select(GitHubConnection.id)
            .where(GitHubConnection.github_user_id == github_user_id, GitHubConnection.user_id != user_id)
            .limit(1)
        )
        return result.first() is not None

    async def upsert_connection(
        self,
        user_id: uuid.UUID,
        *,
        github_user_id: int,
        login: str,
        avatar_url: str | None,
        scopes: list[str],
        access_token_encrypted: str,
    ) -> tuple[GitHubConnection, str | None]:
        """Stores the user's connection and returns it with the encrypted
        token it replaced (None when there was none), so the caller can revoke
        exactly that token."""
        await self._lock_user_connection(user_id)
        result = await self._execute(
            select(GitHubConnection.access_token_encrypted).where(GitHubConnection.user_id == user_id)
        )
        previous_token_encrypted = result.scalar_one_or_none()
        values = {
            "github_user_id": github_user_id,
            "login": login,
            "avatar_url": avatar_url,
            "scopes": scopes,
            "access_token_encrypted": access_token_encrypted,
            "connected_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
        await self._execute(
            pg_insert(GitHubConnection)
            .values(user_id=user_id, **values)
            .on_conflict_do_update(index_elements=[GitHubConnection.user_id], set_=values)
        )
        await self._commit()
        connection = await self.get_connection(user_id)
        await self.release()
        return connection, previous_token_encrypted

    async def _lock_user_connection(self, user_id: uuid.UUID) -> None:
        """Serialises connection writes for one user until the transaction
        ends, so two connect flows finishing at once each see the token the
        other stored (read committed) and none is replaced unrevoked.
        Best effort: without the lock the upsert still runs, just unserialised."""
        try:
            async with self._session.begin_nested():
                await self._session.execute(
                    select(func.pg_advisory_xact_lock(func.hashtext(f"github-connection:{user_id}")))
                )
        except SQLAlchemyError:
            logger.warning("Could not take the GitHub connection lock - continuing without it", exc_info=True)

    async def delete_connection(self, user_id: uuid.UUID) -> None:
        await self._execute(delete(GitHubConnection).where(GitHubConnection.user_id == user_id))
        await self._commit()

    async def release(self) -> None:
        """End the read transaction so the pooled DB connection is not held
        while slow GitHub calls run. Loaded objects stay usable
        (expire_on_commit=False)."""
        try:
            await self._session.commit()
        except SQLAlchemyError as exc:
            await self._session.rollback()
            logger.error("Could not release the database transaction", exc_info=True)
            raise DatabaseError("Could not read data from the database right now. Please try again shortly.") from exc

    # ------------------------------------------------------------------ #
    # OAuth state
    # ------------------------------------------------------------------ #
    async def create_state(
        self, user_id: uuid.UUID, state_hash: str, return_to: str, ttl: timedelta
    ) -> None:
        now = datetime.now(timezone.utc)
        # Parallel flows (double click, two tabs) must all stay valid, so only
        # the user's oldest states beyond MAX_LIVE_STATES - 1 are dropped to
        # make room. Expired rows of anyone are purged a day after expiry.
        newest = (
            select(GitHubOAuthState.state_hash)
            .where(GitHubOAuthState.user_id == user_id)
            .order_by(GitHubOAuthState.created_at.desc())
            .limit(MAX_LIVE_STATES - 1)
        )
        await self._execute(
            delete(GitHubOAuthState)
            .where(GitHubOAuthState.user_id == user_id, GitHubOAuthState.state_hash.not_in(newest))
            .execution_options(synchronize_session=False)
        )
        await self._execute(
            delete(GitHubOAuthState)
            .where(GitHubOAuthState.expires_at < now - timedelta(days=1))
            .execution_options(synchronize_session=False)
        )
        self._session.add(
            GitHubOAuthState(state_hash=state_hash, user_id=user_id, return_to=return_to, expires_at=now + ttl)
        )
        await self._commit()

    async def consume_state(self, user_id: uuid.UUID, state_hash: str) -> str | None:
        """Atomically marks the state used and returns its return_to - only if
        it exists, belongs to this user, is unexpired and was never used."""
        now = datetime.now(timezone.utc)
        result = await self._execute(
            update(GitHubOAuthState)
            .where(
                GitHubOAuthState.state_hash == state_hash,
                GitHubOAuthState.user_id == user_id,
                GitHubOAuthState.used_at.is_(None),
                GitHubOAuthState.expires_at > now,
            )
            .values(used_at=now)
            .returning(GitHubOAuthState.return_to)
            .execution_options(synchronize_session=False)
        )
        return_to = result.scalar_one_or_none()
        await self._commit()
        return return_to
