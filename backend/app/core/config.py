"""Application settings, read from environment variables (see backend/.env.example)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM
    gemini_api_key: str = ""
    gemini_model: str = "gemini-flash-latest"
    gemini_min_interval_s: float = 0.0  # spacing between LLM calls, helps on the free tier

    # Cloudflare Clef (judgement layer for L6/L7; without it the old rules decide)
    cloudflare_account_id: str = ""
    cloudflare_api_token: str = ""
    clef_model: str = "clef-flash"  # or "clef"
    clef_enabled: bool = True

    # Supabase
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    supabase_jwt_secret: str = ""  # only for projects still on the legacy HS256 secret
    database_url: str = ""  # Postgres connection string (Supabase session pooler)

    # Auth: only ever true for local development
    auth_disabled: bool = False

    # Tools
    google_maps_api_key: str = ""
    overpass_url: str = "https://overpass-api.de/api/interpreter"
    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    gmail_refresh_token: str = ""
    gmail_sender: str = ""

    # Demo / safety
    demo_mode: bool = True
    demo_recipients: str = ""  # comma separated
    nudge_after_days: float = 3.0
    tool_retry_backoff_s: float = 1.5

    # Web
    cors_origins: str = "http://localhost:5173"
    public_api_url: str = "http://localhost:8000"

    @property
    def demo_recipient_list(self) -> list[str]:
        return [r.strip() for r in self.demo_recipients.split(",") if r.strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
