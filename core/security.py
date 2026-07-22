import logging
import random
import string
from datetime import datetime, timedelta, timezone
from typing import Any

from jose import JWTError, jwt
from passlib.context import CryptContext
from passlib.exc import PasslibSecurityError

from core.config import Settings

logger = logging.getLogger("webnest.security")

BCRYPT_MAX_PASSWORD_BYTES = 72


class PasswordHasher:
    """Wraps password hashing/verification so callers never touch bcrypt directly."""

    def __init__(self) -> None:
        self._context = CryptContext(schemes=["bcrypt"], deprecated="auto")

    def hash(self, plain_password: str) -> str:
        if not isinstance(plain_password, str) or not plain_password:
            raise ValueError("Password must be a non-empty string")
        if len(plain_password.encode("utf-8")) > BCRYPT_MAX_PASSWORD_BYTES:
            raise ValueError(f"Password must be at most {BCRYPT_MAX_PASSWORD_BYTES} bytes long")
        try:
            return self._context.hash(plain_password)
        except (ValueError, PasslibSecurityError) as exc:
            logger.error("Password hashing failed", exc_info=True)
            raise ValueError("Could not process this password") from exc

    def verify(self, plain_password: str, password_hash: str) -> bool:
        if not plain_password or not password_hash:
            return False
        try:
            return self._context.verify(plain_password, password_hash)
        except (ValueError, PasslibSecurityError):
            logger.warning("Password verification failed due to malformed hash or input", exc_info=True)
            return False


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

    def create_refresh_token(self, subject: str) -> tuple[str, datetime]:
        if not subject:
            raise ValueError("Cannot create a refresh token without a subject")
        try:
            expire = datetime.now(timezone.utc) + timedelta(days=self._settings.refresh_token_expire_days)
            payload = {"sub": subject, "exp": expire, "type": "refresh"}
            token = jwt.encode(payload, self._settings.jwt_secret_key, algorithm=self._settings.jwt_algorithm)
            return token, expire
        except JWTError as exc:
            logger.error("Failed to create refresh token", exc_info=True)
            raise ValueError("Could not create refresh token") from exc

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
        return "".join(random.choices(string.digits, k=length))

    def expiry(self) -> datetime:
        return datetime.now(timezone.utc) + timedelta(minutes=self._settings.otp_expire_minutes)
