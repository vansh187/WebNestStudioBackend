import asyncio
import logging
import secrets
import string
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from jose import ExpiredSignatureError, JWTError, jwt
from passlib.context import CryptContext
from passlib.exc import PasslibSecurityError

from core.config import Settings

logger = logging.getLogger("webnest.security")

BCRYPT_MAX_PASSWORD_BYTES = 72
# The scope claim of a VS Code token. Website routes refuse any token with a scope, so such a
# token works only where a route asks for that scope explicitly (/api/extension/*).
EXTENSION_SCOPE = "extension"


class PasswordHasher:
    """Wraps password hashing/verification so callers never touch bcrypt directly.

    bcrypt is deliberately CPU-slow (~100ms+ per call), so hash()/verify() are
    async and run the actual work in a worker thread via asyncio.to_thread -
    calling bcrypt inline on the event loop would stall every other in-flight
    request on this worker for the duration of each hash/verify.
    """

    def __init__(self) -> None:
        self._context = CryptContext(schemes=["bcrypt"], deprecated="auto")

    def _hash_sync(self, plain_password: str) -> str:
        try:
            return self._context.hash(plain_password)
        except (ValueError, PasslibSecurityError) as exc:
            logger.error("Password hashing failed", exc_info=True)
            raise ValueError("Could not process this password") from exc

    def _verify_sync(self, plain_password: str, password_hash: str) -> bool:
        try:
            return self._context.verify(plain_password, password_hash)
        except (ValueError, PasslibSecurityError):
            logger.warning("Password verification failed due to malformed hash or input", exc_info=True)
            return False

    async def hash(self, plain_password: str) -> str:
        if not isinstance(plain_password, str) or not plain_password:
            raise ValueError("Password must be a non-empty string")
        if len(plain_password.encode("utf-8")) > BCRYPT_MAX_PASSWORD_BYTES:
            raise ValueError(f"Password must be at most {BCRYPT_MAX_PASSWORD_BYTES} bytes long")
        return await asyncio.to_thread(self._hash_sync, plain_password)

    async def verify(self, plain_password: str, password_hash: str) -> bool:
        if not plain_password or not password_hash:
            return False
        return await asyncio.to_thread(self._verify_sync, plain_password, password_hash)


class JWTHandler:
    """Creates and validates access/refresh JWTs for a single configured secret."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def create_access_token(self, subject: str, role: str) -> str:
        if not subject:
            raise ValueError("Cannot create an access token without a subject")
        try:
            expire = datetime.now(timezone.utc) + timedelta(minutes=self._settings.access_token_expire_minutes)
            payload = {"sub": subject, "role": role, "exp": expire, "type": "access"}
            return jwt.encode(payload, self._settings.jwt_secret_key, algorithm=self._settings.jwt_algorithm)
        except JWTError as exc:
            logger.error("Failed to create access token", exc_info=True)
            raise ValueError("Could not create access token") from exc

    def create_refresh_token(self, subject: str, expires_at: datetime | None = None) -> tuple[str, datetime]:
        """expires_at pins the token to an existing session's expiry (used on
        rotation); when omitted a new session window starts now."""
        if not subject:
            raise ValueError("Cannot create a refresh token without a subject")
        try:
            expire = expires_at or datetime.now(timezone.utc) + timedelta(minutes=self._settings.refresh_token_expire_minutes)
            # jti keeps rotated tokens distinct even when they share sub + exp.
            payload = {"sub": subject, "exp": expire, "type": "refresh", "jti": uuid.uuid4().hex}
            token = jwt.encode(payload, self._settings.jwt_secret_key, algorithm=self._settings.jwt_algorithm)
            return token, expire
        except JWTError as exc:
            logger.error("Failed to create refresh token", exc_info=True)
            raise ValueError("Could not create refresh token") from exc

    def create_extension_access_token(self, subject: str, role: str, session_id: str, minutes: int) -> str:
        """A VS Code access token: the usual access claims plus `scope` and `sid`, the session
        it belongs to, so revoking the session ends it before it expires."""
        if not subject or not session_id:
            raise ValueError("Cannot create an extension token without a subject and a session")
        try:
            expire = datetime.now(timezone.utc) + timedelta(minutes=max(minutes, 1))
            payload = {
                "sub": subject,
                "role": role,
                "type": "access",
                "scope": EXTENSION_SCOPE,
                "sid": session_id,
                "exp": expire,
            }
            return jwt.encode(payload, self._settings.jwt_secret_key, algorithm=self._settings.jwt_algorithm)
        except JWTError as exc:
            logger.error("Failed to create extension access token", exc_info=True)
            raise ValueError("Could not create access token") from exc

    def decode_token_or_reason(self, token: str) -> tuple[dict[str, Any] | None, str | None]:
        """(claims, None) for a valid token, else (None, "expired" or "invalid"), so a caller
        can tell a token worth refreshing from one to throw away."""
        if not token or not isinstance(token, str):
            return None, "invalid"
        try:
            claims = jwt.decode(token, self._settings.jwt_secret_key, algorithms=[self._settings.jwt_algorithm])
        except ExpiredSignatureError:
            return None, "expired"
        except (JWTError, ValueError, TypeError):
            return None, "invalid"
        return (claims, None) if isinstance(claims, dict) else (None, "invalid")

    def decode_token(self, token: str) -> dict[str, Any]:
        if not token or not isinstance(token, str):
            raise ValueError("Invalid or expired token")
        try:
            return jwt.decode(token, self._settings.jwt_secret_key, algorithms=[self._settings.jwt_algorithm])
        except JWTError as exc:
            raise ValueError("Invalid or expired token") from exc


class OtpGenerator:
    """Generates numeric OTP codes and their expiry timestamps."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def generate_code(self, length: int = 6) -> str:
        if length <= 0:
            raise ValueError("OTP length must be a positive integer")
        # secrets, not random: the code is a credential and must not be
        # predictable from earlier codes.
        return "".join(secrets.choice(string.digits) for _ in range(length))

    def expiry(self) -> datetime:
        return datetime.now(timezone.utc) + timedelta(minutes=self._settings.otp_expire_minutes)
