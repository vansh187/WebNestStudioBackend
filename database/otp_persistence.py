import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import DatabaseError
from database.base_persistence import BasePersistence
from database.models import OtpFailedAttempt, OtpVerification

logger = logging.getLogger("webnest.database")

FALLBACK_COUNTER_MAX_ENTRIES = 10_000


class FallbackAttemptCounter:
    """In-process wrong-guess counts, used only while the otp_failed_attempts
    table cannot be written (missing table, connection blip). Keeps a user's
    typo from locking their code on the first miss during such an outage while
    still bounding guesses. Per process and lost on restart, which is fine for
    a code that lives ten minutes."""

    def __init__(self, max_entries: int = FALLBACK_COUNTER_MAX_ENTRIES) -> None:
        self._counts: dict[uuid.UUID, int] = {}
        self._max_entries = max(max_entries, 1)

    def increment(self, otp_id: uuid.UUID) -> int:
        count = self._counts.pop(otp_id, 0) + 1
        if len(self._counts) >= self._max_entries:
            # Dicts keep insertion order: drop the entry touched longest ago.
            self._counts.pop(next(iter(self._counts)), None)
        self._counts[otp_id] = count
        return count

    def forget(self, otp_id: uuid.UUID) -> None:
        self._counts.pop(otp_id, None)


_fallback_attempts = FallbackAttemptCounter()


class OtpPersistence(BasePersistence):
    """CRUD access to the otp_verifications table."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def create(
        self,
        email: str,
        otp_code: str,
        purpose: str,
        expires_at: datetime,
        user_id: uuid.UUID | None = None,
    ) -> OtpVerification:
        otp = OtpVerification(email=email, otp_code=otp_code, purpose=purpose, expires_at=expires_at, user_id=user_id)
        self._session.add(otp)
        await self._commit()
        await self._refresh(otp)
        return otp

    async def issue(
        self,
        email: str,
        otp_code: str,
        purpose: str,
        expires_at: datetime,
        user_id: uuid.UUID | None = None,
    ) -> OtpVerification:
        """Creates a code and, in the same commit, retires every earlier
        unused code for this email+purpose - so exactly one code is ever live
        and locking it leaves nothing older to fall back to."""
        await self._execute(self._retire_statement(email, purpose))
        return await self.create(email, otp_code, purpose, expires_at, user_id)

    async def lock_issuance(self, email: str, purpose: str) -> None:
        """Serialises code issuance for one email+purpose until the current
        transaction ends, so concurrent resend requests cannot all pass the
        cooldown and hourly checks before any of them has inserted a code.
        Best effort: without the lock the checks still run, just unserialised."""
        try:
            async with self._session.begin_nested():
                await self._session.execute(
                    select(func.pg_advisory_xact_lock(func.hashtext(f"otp-issue:{purpose}:{email}")))
                )
        except SQLAlchemyError:
            logger.warning("Could not take the OTP issuance lock - continuing without it", exc_info=True)

    async def get_latest_active(self, email: str, purpose: str) -> OtpVerification | None:
        """The newest unused, unexpired code."""
        result = await self._execute(
            select(OtpVerification)
            .where(
                OtpVerification.email == email,
                OtpVerification.purpose == purpose,
                OtpVerification.consumed.is_(False),
                OtpVerification.expires_at > datetime.now(timezone.utc),
            )
            .order_by(OtpVerification.created_at.desc())
            .limit(1)
        )
        return result.scalars().first()

    async def latest_created_at(self, email: str, purpose: str) -> datetime | None:
        """When the most recent code was issued, used or not. The resend
        cooldown reads this rather than the latest *unused* code, otherwise
        burning a code with wrong guesses would skip the cooldown."""
        result = await self._execute(
            select(func.max(OtpVerification.created_at)).where(
                OtpVerification.email == email, OtpVerification.purpose == purpose
            )
        )
        return result.scalar_one_or_none()

    async def count_created_since(self, email: str, purpose: str, since: datetime) -> int:
        """How many codes were issued for this email+purpose since `since`,
        consumed or not."""
        result = await self._execute(
            select(func.count())
            .select_from(OtpVerification)
            .where(
                OtpVerification.email == email,
                OtpVerification.purpose == purpose,
                OtpVerification.created_at >= since,
            )
        )
        return int(result.scalar_one() or 0)

    async def failed_guesses_since(self, email: str, purpose: str, since: datetime) -> int | None:
        """Total wrong guesses against codes issued for this email+purpose
        since `since`. None when the counter table cannot be read; the caller
        then falls back to a cruder limit. Runs in a savepoint so a failure
        does not abort the caller's transaction."""
        try:
            async with self._session.begin_nested():
                result = await self._session.execute(
                    select(func.coalesce(func.sum(OtpFailedAttempt.failed_attempts), 0))
                    .select_from(OtpFailedAttempt)
                    .join(OtpVerification, OtpVerification.id == OtpFailedAttempt.otp_id)
                    .where(
                        OtpVerification.email == email,
                        OtpVerification.purpose == purpose,
                        OtpVerification.created_at >= since,
                    )
                )
                return int(result.scalar_one() or 0)
        except SQLAlchemyError:
            logger.error("Could not read the failed OTP attempt counter", exc_info=True)
            return None

    async def mark_consumed(self, otp: OtpVerification) -> OtpVerification:
        otp.consumed = True
        await self._commit()
        await self._refresh(otp)
        return otp

    async def consume_if_active(self, otp_id: uuid.UUID, commit: bool = True) -> bool:
        """Atomically uses up the code. False means it was already used or
        locked (possibly by a concurrent request), so the caller must reject.

        With commit=False the claim stays in the open transaction, so the
        caller's next write (the password change, the verified flag) commits
        or rolls back together with it - a failed write does not burn the code."""
        result = await self._execute(
            update(OtpVerification)
            .where(OtpVerification.id == otp_id, OtpVerification.consumed.is_(False))
            .values(consumed=True)
            .returning(OtpVerification.id)
            .execution_options(synchronize_session=False)
        )
        claimed = result.scalar_one_or_none() is not None
        if commit:
            await self._commit()
        return claimed

    async def record_failed_attempt(self, otp_id: uuid.UUID, email: str, purpose: str, max_attempts: int) -> int:
        """Counts one wrong guess against the code and, once max_attempts is
        reached, locks every unused code for this email+purpose. Returns the
        running count.

        The increment is a single upsert so concurrent guesses cannot both
        read the same count. If the counter table cannot be written the count
        is kept in process memory instead, so guesses stay bounded without
        locking a user out on their first typo."""
        attempts: int | None = None
        try:
            result = await self._session.execute(
                pg_insert(OtpFailedAttempt)
                .values(otp_id=otp_id, failed_attempts=1)
                .on_conflict_do_update(
                    index_elements=[OtpFailedAttempt.otp_id],
                    set_={
                        "failed_attempts": OtpFailedAttempt.failed_attempts + 1,
                        "updated_at": func.now(),
                    },
                )
                .returning(OtpFailedAttempt.failed_attempts)
            )
            attempts = int(result.scalar_one())
            if attempts >= max_attempts:
                await self._session.execute(self._retire_statement(email, purpose))
            await self._session.commit()
            return attempts
        except SQLAlchemyError:
            logger.error("Could not record a failed OTP attempt - counting in memory instead", exc_info=True)

        try:
            await self._session.rollback()
            # If the upsert itself worked and only the lock/commit failed, the
            # database count is the better one; never go below it.
            attempts = max(attempts or 0, _fallback_attempts.increment(otp_id))
            if attempts >= max_attempts:
                await self._session.execute(self._retire_statement(email, purpose))
                await self._session.commit()
                _fallback_attempts.forget(otp_id)
        except SQLAlchemyError as exc:
            await self._session.rollback()
            logger.error("Could not lock the OTP after too many failed attempts", exc_info=True)
            raise DatabaseError("Could not verify the code right now. Please try again shortly.") from exc
        return attempts

    def _retire_statement(self, email: str, purpose: str):
        return (
            update(OtpVerification)
            .where(
                OtpVerification.email == email,
                OtpVerification.purpose == purpose,
                OtpVerification.consumed.is_(False),
            )
            .values(consumed=True)
            .execution_options(synchronize_session=False)
        )
