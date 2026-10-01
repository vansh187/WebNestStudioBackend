from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central application configuration, loaded from environment / .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    supabase_url: str
    jwt_secret_key: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    # Absolute session lifetime: refresh tokens rotate but keep the expiry set
    # at login, so a user is fully logged out this long after signing in.
    refresh_token_expire_minutes: int = 60
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
    frontend_base_url: str = "https://www.webneststudio.co.in"

    # AI Page Builder: Gemini (primary) + Groq (fallback) generation.
    gemini_api_key: str = ""
    groq_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash-lite"
    # llama-3.3-70b-versatile was retired by Groq (requests returned 404), which
    # silently left every feature without its fallback provider.
    groq_model: str = "openai/gpt-oss-120b"
    rate_limit_per_hour: int = 5
    max_prompt_length: int = 500
    max_refinement_length: int = 300
    # A fully styled single-page site with real CSS/JS routinely needs more than
    # a couple minutes' worth of typical chat-completion latency to finish
    # generating - a low timeout here makes a genuinely successful (if slow)
    # completion look identical to a hung provider.
    llm_timeout_seconds: float = 45.0
    # Comfortably under Gemini flash's 65536 per-request cap - a fully styled
    # single-page site with real CSS/JS can otherwise get cut off mid-generation,
    # which used to silently produce a truncated/blank page (see
    # extract_html_document's </html> check, which now catches this class of
    # failure as a hard error instead).
    gemini_max_output_tokens: int = 16384
    # Groq's llama-3.3-70b-versatile supports up to 32768 completion tokens, but
    # the account-level TPM (tokens-per-minute) budget is what actually binds:
    # Groq rejects a request outright (413) if the requested max_tokens alone
    # could exceed the remaining TPM budget, regardless of how much it would
    # really use. Kept well under the observed 12000 TPM cap on this account.
    groq_max_output_tokens: int = 8000

    # Automated blog generation. Kept default-on for prod; set to False in local
    # .env to avoid burning real Gemini/Groq quota and creating test posts every
    # time the server restarts during development.
    blog_generation_enabled: bool = True
    blog_generation_interval_days: int = 7
    blog_generation_hour_ist: int = 6
    # Vercel deploy hook for the pre-rendered frontend. Called after a post is
    # published, updated or unpublished so the static blog pages are rebuilt.
    # Empty string disables it (local dev).
    vercel_deploy_hook_url: str = ""
    vercel_deploy_hook_timeout_seconds: float = 10.0
    # One-time launch post trigger, e.g. "2026-07-26T18:00:00+05:30". Empty
    # string disables it. Left as a plain settings field (not hardcoded into
    # the scheduler) so it can be set/cleared via env var without a redeploy,
    # and so it naturally becomes a no-op once the date has passed and a post
    # already exists for that day.
    blog_launch_special_post_at: str = ""
    # Where the "new blog post is live" publicity-reminder email goes every
    # time a post (scheduled or manually triggered) is successfully published.
    blog_publish_notification_email: str = "webneststudio19@gmail.com"

    # Automated CodeLab problem generation: each run publishes one easy, one
    # medium and one hard Python problem, every codelab_generation_interval_days
    # (1 = daily, 2 = every other day). Set codelab_generation_enabled to False
    # in local .env so development restarts don't publish problems.
    codelab_generation_enabled: bool = True
    codelab_generation_interval_days: int = 1
    codelab_generation_hour_ist: int = 7
    # Per-test time limit for the generated reference solutions.
    codelab_solution_timeout_seconds: float = 4.0

    # Project Enquiry Chatbot: gathers project requirements from a logged-in
    # user via conversational turns, then produces a week-by-week build plan
    # (no pricing). Reuses the same Gemini/Groq provider account and API keys
    # as the AI Page Builder above - no separate credentials needed, just its
    # own rate limit and length caps so the two features don't share one bucket.
    chatbot_enabled: bool = True
    chatbot_rate_limit_per_hour: int = 10
    chatbot_max_message_length: int = 1000
    chatbot_max_transcript_messages: int = 20
    chatbot_plan_max_weeks: int = 20

    # Coding platform compiler proxy. Executed code runs on JDoodle's hosted
    # sandbox (https://www.jdoodle.com/compiler-api), never inside this API process.
    jdoodle_client_id: str = ""
    jdoodle_client_secret: str = ""
    jdoodle_base_url: str = "https://api.jdoodle.com/v1"
    compiler_request_timeout_seconds: float = 20.0
    compiler_output_limit_bytes: int = 65536
    compiler_source_limit_bytes: int = 131072
    coding_project_limit_per_user: int = 100
    coding_project_source_limit_bytes: int = 262144

    # Java 17 playground: a private Cloud Run service that only accepts calls
    # carrying a Google ID token minted for exactly java_playground_url.
    # java_playground_invoker_key is the full service-account JSON key pasted
    # as one value - a secret: never log it or return it to a client.
    # java_playground_daily_cap is the site-wide run budget per UTC day that
    # keeps the Cloud Run bill at zero.
    java_playground_url: str = ""
    java_playground_invoker_key: str = ""
    java_playground_timeout_seconds: float = 65.0
    java_playground_daily_cap: int = 1500

    # In-app project chat (user-to-user messaging) attachment storage. Files
    # live in a private Supabase Storage bucket; the backend uses the
    # service-role key to mint short-lived signed upload/download URLs so the
    # app never holds a storage credential. supabase_project_url is the
    # Storage REST host (https://<ref>.supabase.co) and is DIFFERENT from
    # supabase_url, which is the Postgres connection string. When these are
    # left blank the messaging feature still runs but attachment endpoints
    # return 503.
    supabase_project_url: str = ""
    supabase_service_role_key: str = ""
    chat_storage_bucket: str = "chat-attachments"
    chat_max_attachment_mb: int = 25

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
