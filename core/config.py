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

    # Temporary switch while the sending domain is pending at the registrar:
    # False disables all outbound email (OTP + lead notifications) and makes
    # signup auto-verify accounts / login skip the verification check, so the
    # product keeps working without email. Flip to True (env var only, no
    # code change) once the domain is verified with the email provider.
    email_enabled: bool = False

    resend_api_key: str = ""
    resend_from_address: str = "onboarding@resend.dev"
    team_notification_email: str = ""

    cors_origins: str = "*"

    # Public site origin used to build absolute URLs in sitemap.xml / robots.txt.
    frontend_base_url: str = "https://webneststudio.co.in"

    # AI Page Builder: Gemini (primary) + Groq (fallback) generation.
    gemini_api_key: str = ""
    groq_api_key: str = ""
    gemini_model: str = "gemini-flash-latest"
    groq_model: str = "llama-3.3-70b-versatile"
    rate_limit_per_hour: int = 5
    max_prompt_length: int = 500
    max_refinement_length: int = 300
    llm_timeout_seconds: float = 20.0
    llm_max_output_tokens: int = 8192

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
