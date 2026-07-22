from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central application configuration, loaded from environment / .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    supabase_url: str
    jwt_secret_key: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 30
    otp_expire_minutes: int = 10

    resend_api_key: str = ""
    resend_from_address: str = "onboarding@resend.dev"
    team_notification_email: str = ""

    cors_origins: str = "*"

    @property
    def async_database_url(self) -> str:
        raw = self.supabase_url.strip()
        if raw.startswith("//"):
            raw = "postgresql://" + raw.lstrip("/")
        if raw.startswith("postgresql://"):
            raw = raw.replace("postgresql://", "postgresql+asyncpg://", 1)
        elif raw.startswith("postgres://"):
            raw = raw.replace("postgres://", "postgresql+asyncpg://", 1)
        return raw

    @property
    def cors_origin_list(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
