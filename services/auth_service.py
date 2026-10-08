import hashlib
import hmac
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
MAX_OTP_ATTEMPTS = 5
# Wrong guesses allowed per email+purpose per hour before no new code is
# issued. This, not the number of codes, is what bounds brute force - and a
# count of guesses cannot be used up by someone who merely keeps pressing
# "resend" on another person's account.
MAX_FAILED_GUESSES_PER_HOUR = 25
# Used only when the guess counter cannot be read.
MAX_OTPS_PER_HOUR = 5
INVALID_OTP_MESSAGE = "Invalid or expired OTP code"
TOO_MANY_ATTEMPTS_MESSAGE = "Too many incorrect attempts for this account. Please try again in an hour"


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

    def _codes_match(self, stored: object, supplied: object) -> bool:
        if not isinstance(stored, str) or not isinstance(supplied, str) or not stored:
            return False
        return hmac.compare_digest(stored.encode("utf-8"), supplied.encode("utf-8"))

    async def _require_matching_otp(self, email: str, purpose: str, otp_code: str) -> uuid.UUID:
        """Returns the id of the live code for this email+purpose if otp_code
        matches it. A wrong guess is counted, and the code is locked after
        MAX_OTP_ATTEMPTS misses - without that, a 6-digit code can simply be
        guessed. The caller still has to claim the code with consume_if_active.

        An expired code is treated exactly like no code: same message, nothing
        counted, nothing written."""
        otp = await self._otps.get_latest_active(email, purpose)
        if otp is None or otp.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
            raise ValidationError(INVALID_OTP_MESSAGE)
        # Read the id up front: recording a miss commits (or rolls back),
        # after which the row must not be touched again.
        otp_id = otp.id

        if not self._codes_match(otp.otp_code, otp_code):
            attempts = await self._otps.record_failed_attempt(otp_id, email, purpose, MAX_OTP_ATTEMPTS)
            if attempts >= MAX_OTP_ATTEMPTS:
                raise ValidationError("Too many incorrect attempts. Please request a new code")
            raise ValidationError(INVALID_OTP_MESSAGE)
        return otp_id

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

        otp_id = await self._require_matching_otp(email, purpose, otp_code)
        user = await self._users.get_by_email(email)
        if user is None:
            raise ValidationError("No account found for this email")
        # The claim is committed by mark_verified, together with the verified
        # flag: if that write fails the code is still usable.
        if not await self._otps.consume_if_active(otp_id, commit=False):
            raise ValidationError(INVALID_OTP_MESSAGE)
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
        email = user.email

        # Held until the new code is committed, so two concurrent requests
        # cannot both pass the checks below.
        await self._otps.lock_issuance(email, purpose)

        now = datetime.now(timezone.utc)
        last_issued_at = await self._otps.latest_created_at(email, purpose)
        if last_issued_at is not None:
            age = now - last_issued_at.replace(tzinfo=timezone.utc)
            if age < timedelta(seconds=RESEND_OTP_COOLDOWN_SECONDS):
                wait_seconds = RESEND_OTP_COOLDOWN_SECONDS - max(int(age.total_seconds()), 0)
                raise RateLimitedError(f"Please wait {wait_seconds}s before requesting another code")

        await self._require_guess_budget(email, purpose, now - timedelta(hours=1))

        otp_code = self._otp_generator.generate_code()
        await self._otps.issue(
            email=email, otp_code=otp_code, purpose=purpose, expires_at=self._otp_generator.expiry(), user_id=user.id
        )
        return otp_code

    async def _require_guess_budget(self, email: str, purpose: str, since: datetime) -> None:
        """Refuses a new code once too many wrong guesses were made against
        this email+purpose since `since`. Requesting codes alone never trips
        it, so the limit cannot be used to lock someone else out of their own
        password reset just by pressing "resend"."""
        failed_guesses = await self._otps.failed_guesses_since(email, purpose, since)
        if failed_guesses is None:
            # Counter unavailable: fall back to capping the number of codes.
            if await self._otps.count_created_since(email, purpose, since) >= MAX_OTPS_PER_HOUR:
                raise RateLimitedError(TOO_MANY_ATTEMPTS_MESSAGE)
            return
        if failed_guesses >= MAX_FAILED_GUESSES_PER_HOUR:
            raise RateLimitedError(TOO_MANY_ATTEMPTS_MESSAGE)

    async def reset_password(self, email: str, otp_code: str, new_password: str) -> None:
        if not self._settings.email_enabled:
            raise ValidationError("Email verification is currently disabled; accounts are auto-verified at signup")

        otp_id = await self._require_matching_otp(email, "password_reset", otp_code)

        user = await self._users.get_by_email(email)
        if user is None:
            raise NotFoundError("No account found for this email")

        try:
            password_hash = await self._password_hasher.hash(new_password)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

        # Claimed atomically and last, so a code that was locked or used by a
        # concurrent request while we were hashing cannot still set a password.
        # The claim, the new password and the token revocation are one commit
        # (made by revoke_all_for_user): if any of it fails, nothing changed
        # and the code is still usable.
        user_id = user.id
        if not await self._otps.consume_if_active(otp_id, commit=False):
            raise ValidationError(INVALID_OTP_MESSAGE)
        await self._users.update_password_hash(user, password_hash, commit=False)
        # A stolen refresh token shouldn't survive its owner resetting their
        # password because they suspected exactly that.
        await self._refresh_tokens.revoke_all_for_user(user_id)

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
        session_expires_at = stored.expires_at.replace(tzinfo=timezone.utc)
        if session_expires_at < datetime.now(timezone.utc):
            raise UnauthorizedError("Refresh token has expired")

        user = await self._users.get_by_id(stored.user_id)
        if user is None or not user.is_active:
            raise UnauthorizedError("Account no longer available")

        await self._refresh_tokens.revoke(stored)
        try:
            new_access_token = self._jwt_handler.create_access_token(str(user.id), user.role)
            # Carry the login-time expiry forward so rotation can't extend the session.
            new_refresh_token, expires_at = self._jwt_handler.create_refresh_token(
                str(user.id), expires_at=session_expires_at
            )
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
