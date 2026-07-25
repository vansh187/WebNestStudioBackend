import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 72  # bcrypt hard limit is 72 bytes; enforced in bytes below, not just chars


class SignupRequest(BaseModel):
    full_name: str | None = Field(default=None, max_length=200)
    email: EmailStr
    phone_number: str | None = Field(default=None, max_length=30)
    password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)

    @field_validator("password")
    @classmethod
    def password_must_fit_bcrypt_limit(cls, value: str) -> str:
        if len(value.encode("utf-8")) > PASSWORD_MAX_LENGTH:
            raise ValueError(f"Password must be at most {PASSWORD_MAX_LENGTH} bytes long")
        return value

    @field_validator("full_name")
    @classmethod
    def full_name_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            return None
        return value


class VerifyOtpRequest(BaseModel):
    email: EmailStr
    otp_code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")
    purpose: Literal["signup", "login", "password_reset"] = "signup"


class ResendOtpRequest(BaseModel):
    email: EmailStr
    purpose: Literal["signup", "login", "password_reset"] = "signup"


class ResendOtpResponse(BaseModel):
    message: str


class ResetPasswordRequest(BaseModel):
    email: EmailStr
    otp_code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")
    new_password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)

    @field_validator("new_password")
    @classmethod
    def new_password_must_fit_bcrypt_limit(cls, value: str) -> str:
        if len(value.encode("utf-8")) > PASSWORD_MAX_LENGTH:
            raise ValueError(f"Password must be at most {PASSWORD_MAX_LENGTH} bytes long")
        return value


class ResetPasswordResponse(BaseModel):
    message: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class LogoutRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    id: uuid.UUID
    full_name: str | None
    email: str
    phone_number: str | None
    role: str
    is_verified: bool
    is_active: bool
    last_login_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}
