import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.base_persistence import BasePersistence
from database.models import OtpVerification


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

    async def get_latest_active(self, email: str, purpose: str) -> OtpVerification | None:
        result = await self._execute(
            select(OtpVerification)
            .where(
                OtpVerification.email == email,
                OtpVerification.purpose == purpose,
                OtpVerification.consumed.is_(False),
            )
            .order_by(OtpVerification.created_at.desc())
        )
        return result.scalars().first()

    async def mark_consumed(self, otp: OtpVerification) -> OtpVerification:
        otp.consumed = True
        await self._commit()
        await self._refresh(otp)
        return otp
