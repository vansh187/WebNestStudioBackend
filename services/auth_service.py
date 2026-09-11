import hashlib
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings
from core.exceptions import ConflictError, NotFoundError, RateLimitedError, UnauthorizedError, ValidationError
from core.security import JWTHandler, OtpGenerator, PasswordHasher
from database.models import User
from database.otp_persistence import OtpPersistence
from database.refresh_token_persistence import RefreshTokenPersistence
from database.user_persistence import UserPersistence

RESEND_OTP_COOLDOWN_SECONDS = 60


class AuthService:
    """Signup, OTP verification, login, token refresh and logout flows.

    Does not send email itself: OTP delivery is a slow, best-effort network
    call that must never block the request/response cycle, so callers are
    expected to schedule it (e.g. via FastAPI BackgroundTasks) using the
    otp_code this returns.
    """

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        password_hasher: PasswordHasher,
        jwt_handler: JWTHandler,
        otp_generator: OtpGenerator,
    ) -> None:
        self._users = UserPersistence(session)
        self._otps = OtpPersistence(session)
        self._refresh_tokens = RefreshTokenPersistence(session)
        self._settings = settings
        self._password_hasher = password_hasher
        self._jwt_handler = jwt_handler
        self._otp_generator = otp_generator

    def _hash_token(self, token: str) -> str:
        if not token or not isinstance(token, str):
            raise ValidationError("A valid token string is required")
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    async def signup(self, full_name: str | None, email: str, phone_number: str | None, password: str) -> tuple[User, str | None]:
        existing = await self._users.get_by_email(email)
        if existing is not None:
            raise ConflictError("An account with this email already exists")

        try:
            password_hash = await self._password_hasher.hash(password)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

        user = await self._users.create(full_name, email, phone_number, password_hash)

        if not self._settings.email_enabled:
            # Email verification is temporarily disabled (e.g. sending domain
            # pending) - auto-verify so the account is usable immediately.
            user = await self._users.mark_verified(user)
            return user, None

        otp_code = self._otp_generator.generate_code()
        await self._otps.create(email=email, otp_code=otp_code, purpose="signup", expires_at=self._otp_generator.expiry(), user_id=user.id)
        return user, otp_code

    async def verify_otp(self, email: str, otp_code: str, purpose: str = "signup") -> User:
        if not self._settings.email_enabled:
            raise ValidationError("Email verification is currently disabled; accounts are auto-verified at signup")

        otp = await self._otps.get_latest_active(email, purpose)
        if otp is None or otp.otp_code != otp_code:
            raise ValidationError("Invalid or expired OTP code")
        if otp.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
            raise ValidationError("Invalid or expired OTP code")

        await self._otps.mark_consumed(otp)
        user = await self._users.get_by_email(email)
        if user is None:
            raise ValidationError("No account found for this email")
        return await self._users.mark_verified(user)

    async def resend_otp(self, email: str, purpose: str = "signup") -> str:
        """Issues a fresh OTP for an existing account (e.g. the original signup
        code expired before the user could verify). Rate-limited per email+purpose
        to stop a resend loop from spamming the recipient's inbox or exhausting
        the email provider's send limits."""
        if not self._settings.email_enabled:
            raise ValidationError("Email verification is currently disabled; accounts are auto-verified at signup")

        user = await self._users.get_by_email(email)
        if user is None:
            raise NotFoundError("No account found for this email")
        if purpose == "signup" and user.is_verified:
            raise ValidationError("This account is already verified")

        existing = await self._otps.get_latest_active(email, purpose)
        if existing is not None:
            age = datetime.now(timezone.utc) - existing.created_at.replace(tzinfo=timezone.utc)
            if age < timedelta(seconds=RESEND_OTP_COOLDOWN_SECONDS):
                wait_seconds = RESEND_OTP_COOLDOWN_SECONDS - int(age.total_seconds())
                raise RateLimitedError(f"Please wait {wait_seconds}s before requesting another code")

        otp_code = self._otp_generator.generate_code()
        await self._otps.create(
            email=email, otp_code=otp_code, purpose=purpose, expires_at=self._otp_generator.expiry(), user_id=user.id
        )
        return otp_code

    async def reset_password(self, email: str, otp_code: str, new_password: str) -> None:
        if not self._settings.email_enabled:
            raise ValidationError("Email verification is currently disabled; accounts are auto-verified at signup")

        otp = await self._otps.get_latest_active(email, "password_reset")
        if otp is None or otp.otp_code != otp_code:
            raise ValidationError("Invalid or expired OTP code")
        if otp.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
            raise ValidationError("Invalid or expired OTP code")

        user = await self._users.get_by_email(email)
        if user is None:
            raise NotFoundError("No account found for this email")

        try:
            password_hash = await self._password_hasher.hash(new_password)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

        await self._otps.mark_consumed(otp)
        await self._users.update_password_hash(user, password_hash)
        # A stolen refresh token shouldn't survive its owner resetting their
        # password because they suspected exactly that.
        await self._refresh_tokens.revoke_all_for_user(user.id)

    async def login(
        self, email: str, password: str, user_agent: str | None, ip_address: str | None
    ) -> tuple[str, str, User]:
        if not email or not password:
            raise ValidationError("Email and password are required")

        user = await self._users.get_by_email(email)
        if user is None or not await self._password_hasher.verify(password, user.password_hash):
            raise UnauthorizedError("Invalid email or password")
        if not user.is_active:
            raise UnauthorizedError("This account has been disabled")
        if self._settings.email_enabled and not user.is_verified:
            raise UnauthorizedError("Please verify your email address before logging in")

        try:
            access_token = self._jwt_handler.create_access_token(str(user.id), user.role)
            refresh_token, expires_at = self._jwt_handler.create_refresh_token(str(user.id))
        except ValueError as exc:
            raise UnauthorizedError(str(exc)) from exc
        await self._refresh_tokens.create(
            user_id=user.id,
            token_hash=self._hash_token(refresh_token),
            expires_at=expires_at,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        await self._users.update_last_login(user, datetime.now(timezone.utc))
        return access_token, refresh_token, user

    async def refresh(self, refresh_token: str) -> tuple[str, str]:
        try:
            payload = self._jwt_handler.decode_token(refresh_token)
        except ValueError as exc:
            raise UnauthorizedError("Invalid or expired refresh token") from exc
        if payload.get("type") != "refresh":
            raise UnauthorizedError("Invalid token type")

        token_hash = self._hash_token(refresh_token)
        stored = await self._refresh_tokens.get_by_hash(token_hash)
        if stored is None or stored.revoked:
            raise UnauthorizedError("Refresh token has been revoked")
        if stored.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
            raise UnauthorizedError("Refresh token has expired")

        user = await self._users.get_by_id(stored.user_id)
        if user is None or not user.is_active:
            raise UnauthorizedError("Account no longer available")

        await self._refresh_tokens.revoke(stored)
        try:
            new_access_token = self._jwt_handler.create_access_token(str(user.id), user.role)
            new_refresh_token, expires_at = self._jwt_handler.create_refresh_token(str(user.id))
        except ValueError as exc:
            raise UnauthorizedError(str(exc)) from exc
        await self._refresh_tokens.create(
            user_id=user.id,
            token_hash=self._hash_token(new_refresh_token),
            expires_at=expires_at,
            user_agent=stored.user_agent,
            ip_address=stored.ip_address,
        )
        return new_access_token, new_refresh_token

    async def logout(self, refresh_token: str) -> None:
        stored = await self._refresh_tokens.get_by_hash(self._hash_token(refresh_token))
        if stored is not None:
            await self._refresh_tokens.revoke(stored)

    async def logout_all_devices(self, user_id) -> None:
        await self._refresh_tokens.revoke_all_for_user(user_id)

    async def delete_account(self, user: User) -> None:
        await self._refresh_tokens.revoke_all_for_user(user.id)
        await self._users.delete_account(user)

    async def get_current_user(self, access_token: str) -> User:
        try:
            payload = self._jwt_handler.decode_token(access_token)
        except ValueError as exc:
            raise UnauthorizedError("Invalid or expired access token") from exc
        if payload.get("type") != "access":
            raise UnauthorizedError("Invalid token type")

        try:
            user_id = uuid.UUID(payload["sub"])
        except (KeyError, ValueError) as exc:
            raise UnauthorizedError("Invalid access token payload") from exc

        user = await self._users.get_by_id(user_id)
        if user is None or not user.is_active:
            raise UnauthorizedError("Account no longer available")
        return user
