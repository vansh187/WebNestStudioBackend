"""Login codes, sessions and refresh tokens for WebNest AI in VS Code.

SqlExtensionSessionStore opens its own short session for each method, with a time limit, and
always hands the connection back, so nothing holds a connection while a reply streams and a
stuck database can't hang a request. Codes and refresh tokens are claimed with one
UPDATE ... WHERE used_at IS NULL ... RETURNING each, so two racing requests can never both
succeed. Only SHA-256 hashes of codes and tokens are ever stored.

ExtensionSessionPersistence works inside a caller's session instead (the website's
AuthService), so ending VS Code sessions commits together with a password reset."""

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol, cast

from sqlalchemy import CursorResult, delete, func, insert, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import DatabaseError
from database.base_persistence import BasePersistence
from database.models import ExtensionAuthCode, ExtensionRefreshToken, ExtensionSession, User

# Shorter than the pool's own 30-second wait, so a full pool fails fast too.
DB_TIMEOUT_SECONDS = 10.0
MAX_LISTED_SESSIONS = 100
_READ_ERROR = "Could not read your VS Code session right now. Please try again shortly."
_WRITE_ERROR = "Could not save your VS Code session right now. Please try again shortly."

RevokeReason = Literal["logout", "user", "reuse", "password_reset", "admin"]
OpenStatus = Literal["ok", "disabled", "missing"]
RefreshStatus = Literal["ok", "unknown", "expired", "revoked", "reused", "disabled"]


@dataclass(frozen=True)
class NewAuthCode:
    code_hash: str
    user_id: uuid.UUID
    code_challenge: str
    redirect_uri: str
    device_name: str | None
    app_name: str | None
    ext_version: str | None
    expires_at: datetime


@dataclass(frozen=True)
class ConsumedCode:
    user_id: uuid.UUID
    code_challenge: str
    redirect_uri: str
    device_name: str | None
    app_name: str | None
    ext_version: str | None


@dataclass(frozen=True)
class NewSession:
    user_id: uuid.UUID
    device_name: str | None
    app_name: str | None
    ext_version: str | None
    expires_at: datetime
    token_hash: str
    token_expires_at: datetime


@dataclass(frozen=True)
class OpenResult:
    status: OpenStatus
    session_id: uuid.UUID | None = None
    role: str | None = None


@dataclass(frozen=True)
class RefreshResult:
    status: RefreshStatus
    session_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    role: str | None = None
    token_expires_at: datetime | None = None


@dataclass(frozen=True)
class SessionUser:
    """A session joined to its account, as each extension request checks it."""

    session_id: uuid.UUID
    user_id: uuid.UUID
    email: str
    full_name: str | None
    role: str
    user_active: bool
    revoked: bool
    expires_at: datetime


@dataclass(frozen=True)
class SessionSummary:
    session_id: uuid.UUID
    device_name: str | None
    app_name: str | None
    ext_version: str | None
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime


class ExtensionSessionStore(Protocol):
    async def create_code(self, code: NewAuthCode) -> None: ...

    async def consume_code(self, code_hash: str, now: datetime) -> ConsumedCode | None: ...

    async def open_session(self, new: NewSession, now: datetime) -> OpenResult: ...

    async def refresh(
        self,
        old_hash: str,
        new_hash: str,
        now: datetime,
        grace: timedelta,
        token_lifetime: timedelta,
        ext_version: str | None,
    ) -> RefreshResult: ...

    async def session_user(self, session_id: uuid.UUID) -> SessionUser | None: ...

    async def revoke_by_token(self, token_hash: str, now: datetime, reason: RevokeReason) -> bool: ...

    async def revoke_session(self, user_id: uuid.UUID, session_id: uuid.UUID, now: datetime, reason: RevokeReason) -> bool: ...

    async def revoke_all_for_user(self, user_id: uuid.UUID, now: datetime, reason: RevokeReason) -> int: ...

    async def list_sessions(self, user_id: uuid.UUID, now: datetime) -> list[SessionSummary]: ...

    async def purge(self, codes_and_tokens_before: datetime, sessions_before: datetime) -> int: ...


def _rowcount(result: Any) -> int:
    return int(cast(CursorResult[Any], result).rowcount or 0)


class SqlExtensionSessionStore:
    def __init__(self, session_factory: Callable[[], AsyncSession], timeout_seconds: float = DB_TIMEOUT_SECONDS) -> None:
        self._session_factory = session_factory
        self._timeout_seconds = timeout_seconds

    @asynccontextmanager
    async def _session(self, error_message: str) -> AsyncIterator[AsyncSession]:
        """A session that is always closed, which rolls back anything not committed and
        returns its connection, however the block ends. OSError: a refused connection
        reaches here unwrapped by SQLAlchemy."""
        try:
            async with asyncio.timeout(self._timeout_seconds):
                async with self._session_factory() as session:
                    yield session
        except (SQLAlchemyError, OSError, TimeoutError) as exc:
            raise DatabaseError(error_message) from exc

    async def create_code(self, code: NewAuthCode) -> None:
        statement = insert(ExtensionAuthCode).values(
            code_hash=code.code_hash,
            user_id=code.user_id,
            code_challenge=code.code_challenge,
            redirect_uri=code.redirect_uri,
            device_name=code.device_name,
            app_name=code.app_name,
            ext_version=code.ext_version,
            expires_at=code.expires_at,
        )
        async with self._session(_WRITE_ERROR) as session:
            await session.execute(statement)
            await session.commit()

    async def consume_code(self, code_hash: str, now: datetime) -> ConsumedCode | None:
        """Claims a live code once, atomically: None when it is unknown, expired or used."""
        claim = (
            update(ExtensionAuthCode)
            .where(
                ExtensionAuthCode.code_hash == code_hash,
                ExtensionAuthCode.used_at.is_(None),
                ExtensionAuthCode.expires_at > now,
            )
            .values(used_at=now)
            .returning(
                ExtensionAuthCode.user_id,
                ExtensionAuthCode.code_challenge,
                ExtensionAuthCode.redirect_uri,
                ExtensionAuthCode.device_name,
                ExtensionAuthCode.app_name,
                ExtensionAuthCode.ext_version,
            )
        )
        async with self._session(_WRITE_ERROR) as session:
            row = (await session.execute(claim)).first()
            await session.commit()
        if row is None:
            return None
        return ConsumedCode(
            user_id=row[0],
            code_challenge=row[1],
            redirect_uri=row[2],
            device_name=row[3],
            app_name=row[4],
            ext_version=row[5],
        )

    async def open_session(self, new: NewSession, now: datetime) -> OpenResult:
        """The session and its first refresh token, in one commit, only for an active
        account. The account row is key-share locked for the moment it takes, so it can't be
        deleted between the check and the insert, while ordinary updates to it still go ahead."""
        account = select(User.is_active, User.role).where(User.id == new.user_id).with_for_update(read=True, key_share=True)
        async with self._session(_WRITE_ERROR) as session:
            row = (await session.execute(account)).first()
            if row is None:
                await session.rollback()
                return OpenResult("missing")
            if not row[0]:
                await session.rollback()
                return OpenResult("disabled")
            session_id = (
                await session.execute(
                    insert(ExtensionSession)
                    .values(
                        user_id=new.user_id,
                        device_name=new.device_name,
                        app_name=new.app_name,
                        ext_version=new.ext_version,
                        created_at=now,
                        last_used_at=now,
                        expires_at=new.expires_at,
                    )
                    .returning(ExtensionSession.id)
                )
            ).scalar_one()
            await session.execute(
                insert(ExtensionRefreshToken).values(
                    token_hash=new.token_hash, session_id=session_id, expires_at=new.token_expires_at, created_at=now
                )
            )
            await session.commit()
        return OpenResult("ok", session_id, row[1] or "client")

    async def refresh(
        self,
        old_hash: str,
        new_hash: str,
        now: datetime,
        grace: timedelta,
        token_lifetime: timedelta,
        ext_version: str | None,
    ) -> RefreshResult:
        """Rotates a refresh token in one transaction. A token used again within `grace`
        still gets a new one (two windows refreshing at once, a lost response); used again
        later, it was copied, and the whole session is revoked."""
        claim = (
            update(ExtensionRefreshToken)
            .where(
                ExtensionRefreshToken.token_hash == old_hash,
                ExtensionRefreshToken.used_at.is_(None),
                ExtensionRefreshToken.expires_at > now,
            )
            .values(used_at=now)
            .returning(ExtensionRefreshToken.session_id)
        )
        async with self._session(_WRITE_ERROR) as session:
            claimed = (await session.execute(claim)).first()
            if claimed is not None:
                session_id = claimed[0]
            else:
                known = (
                    await session.execute(
                        select(ExtensionRefreshToken.session_id, ExtensionRefreshToken.used_at).where(
                            ExtensionRefreshToken.token_hash == old_hash
                        )
                    )
                ).first()
                if known is None:
                    return RefreshResult("unknown")
                session_id, used_at = known[0], known[1]
                if used_at is None:
                    return RefreshResult("expired", session_id)
                if now - used_at > grace:
                    await session.execute(
                        update(ExtensionSession)
                        .where(ExtensionSession.id == session_id, ExtensionSession.revoked_at.is_(None))
                        .values(revoked_at=now, revoked_reason="reuse")
                    )
                    await session.commit()
                    return RefreshResult("reused", session_id)
            # Locked, so a disconnect on the website can't slip between this check and the
            # new token.
            state = (
                await session.execute(
                    select(ExtensionSession.user_id, ExtensionSession.revoked_at, ExtensionSession.expires_at, User.is_active, User.role)
                    .join(User, User.id == ExtensionSession.user_id)
                    .where(ExtensionSession.id == session_id)
                    .with_for_update(of=ExtensionSession)
                )
            ).first()
            if state is None:
                await session.commit()
                return RefreshResult("unknown", session_id)
            user_id, revoked_at, expires_at, user_active, role = state
            status: RefreshStatus | None = None
            if not user_active:
                status = "disabled"
            elif revoked_at is not None:
                status = "revoked"
            elif expires_at <= now:
                status = "expired"
            if status is not None:
                # The claim is kept: the token was spent either way.
                await session.commit()
                return RefreshResult(status, session_id, user_id)
            token_expires_at = min(expires_at, now + token_lifetime)
            await session.execute(
                insert(ExtensionRefreshToken).values(
                    token_hash=new_hash, session_id=session_id, expires_at=token_expires_at, created_at=now
                )
            )
            await session.execute(
                update(ExtensionSession)
                .where(ExtensionSession.id == session_id)
                .values(last_used_at=now, ext_version=func.coalesce(ext_version, ExtensionSession.ext_version))
            )
            await session.commit()
        return RefreshResult("ok", session_id, user_id, role or "client", token_expires_at)

    async def session_user(self, session_id: uuid.UUID) -> SessionUser | None:
        statement = (
            select(
                ExtensionSession.id,
                ExtensionSession.user_id,
                User.email,
                User.full_name,
                User.role,
                User.is_active,
                ExtensionSession.revoked_at,
                ExtensionSession.expires_at,
            )
            .join(User, User.id == ExtensionSession.user_id)
            .where(ExtensionSession.id == session_id)
        )
        async with self._session(_READ_ERROR) as session:
            row = (await session.execute(statement)).first()
        if row is None:
            return None
        return SessionUser(
            session_id=row[0],
            user_id=row[1],
            email=row[2],
            full_name=row[3],
            role=row[4] or "client",
            user_active=bool(row[5]),
            revoked=row[6] is not None,
            expires_at=row[7],
        )

    async def revoke_by_token(self, token_hash: str, now: datetime, reason: RevokeReason) -> bool:
        """Ends the session a refresh token belongs to, whatever state the token is in."""
        owner = select(ExtensionRefreshToken.session_id).where(ExtensionRefreshToken.token_hash == token_hash).scalar_subquery()
        statement = (
            update(ExtensionSession)
            .where(ExtensionSession.id == owner, ExtensionSession.revoked_at.is_(None))
            .values(revoked_at=now, revoked_reason=reason)
        )
        async with self._session(_WRITE_ERROR) as session:
            result = await session.execute(statement)
            await session.commit()
        return _rowcount(result) > 0

    async def revoke_session(self, user_id: uuid.UUID, session_id: uuid.UUID, now: datetime, reason: RevokeReason) -> bool:
        """Only the user's own session that is still live: anything else is not found."""
        statement = (
            update(ExtensionSession)
            .where(
                ExtensionSession.id == session_id,
                ExtensionSession.user_id == user_id,
                ExtensionSession.revoked_at.is_(None),
                ExtensionSession.expires_at > now,
            )
            .values(revoked_at=now, revoked_reason=reason)
        )
        async with self._session(_WRITE_ERROR) as session:
            result = await session.execute(statement)
            await session.commit()
        return _rowcount(result) > 0

    async def revoke_all_for_user(self, user_id: uuid.UUID, now: datetime, reason: RevokeReason) -> int:
        statement = (
            update(ExtensionSession)
            .where(ExtensionSession.user_id == user_id, ExtensionSession.revoked_at.is_(None))
            .values(revoked_at=now, revoked_reason=reason)
        )
        async with self._session(_WRITE_ERROR) as session:
            result = await session.execute(statement)
            await session.commit()
        return _rowcount(result)

    async def list_sessions(self, user_id: uuid.UUID, now: datetime) -> list[SessionSummary]:
        statement = (
            select(
                ExtensionSession.id,
                ExtensionSession.device_name,
                ExtensionSession.app_name,
                ExtensionSession.ext_version,
                ExtensionSession.created_at,
                ExtensionSession.last_used_at,
                ExtensionSession.expires_at,
            )
            .where(ExtensionSession.user_id == user_id, ExtensionSession.revoked_at.is_(None), ExtensionSession.expires_at > now)
            .order_by(ExtensionSession.last_used_at.desc(), ExtensionSession.id)
            .limit(MAX_LISTED_SESSIONS)
        )
        async with self._session(_READ_ERROR) as session:
            rows = (await session.execute(statement)).all()
        return [
            SessionSummary(
                session_id=row[0],
                device_name=row[1],
                app_name=row[2],
                ext_version=row[3],
                created_at=row[4],
                last_used_at=row[5],
                expires_at=row[6],
            )
            for row in rows
        ]

    async def purge(self, codes_and_tokens_before: datetime, sessions_before: datetime) -> int:
        """Codes and refresh tokens that expired before the first time, and sessions that
        ended before the second (their tokens go with them)."""
        statements = (
            delete(ExtensionAuthCode).where(ExtensionAuthCode.expires_at < codes_and_tokens_before),
            delete(ExtensionRefreshToken).where(ExtensionRefreshToken.expires_at < codes_and_tokens_before),
            delete(ExtensionSession).where(
                or_(ExtensionSession.revoked_at < sessions_before, ExtensionSession.expires_at < sessions_before)
            ),
        )
        removed = 0
        async with self._session(_WRITE_ERROR) as session:
            for statement in statements:
                removed += _rowcount(await session.execute(statement))
            await session.commit()
        return removed


class ExtensionSessionPersistence(BasePersistence):
    """Inside the caller's session and transaction: nothing here commits, so ending VS Code
    sessions succeeds or fails together with the caller's own change."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def revoke_all_for_user(self, user_id: uuid.UUID, now: datetime, reason: RevokeReason) -> None:
        await self._execute(
            update(ExtensionSession)
            .where(ExtensionSession.user_id == user_id, ExtensionSession.revoked_at.is_(None))
            .values(revoked_at=now, revoked_reason=reason)
        )
