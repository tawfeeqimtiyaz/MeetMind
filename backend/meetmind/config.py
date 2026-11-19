"""All settings come from environment variables (see .env.example).

pydantic-settings maps each field to the env var of the same name, case-insensitively
(e.g. `secret_key` <- SECRET_KEY) and converts types (int, bool) for us.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict

ALLOWED_RECORDING_EXTENSIONS = frozenset({
    ".mp3", ".wav", ".m4a", ".mp4", ".webm", ".ogg", ".flac", ".mpeg", ".mov",
})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    app_env: str = "production"  # development | production | test

    secret_key: str = ""
    database_url: str = ""
    redis_url: str = "redis://redis:6379/0"
    queue_name: str = "meetmind"

    # Public URL of the frontend (used in invite / email links)
    app_base_url: str = "http://localhost:8080"
    cors_origins: str = "http://localhost:8080"  # comma-separated

    # Auth
    jwt_expires_minutes: int = 480
    invite_expires_hours: int = 72
    allow_signup: bool = True

    # Uploads
    upload_dir: str = "/data/uploads"
    max_upload_mb: int = 500

    # Rate limiting (defaults to the Redis above so limits are shared by all API workers)
    ratelimit_enabled: bool = True
    ratelimit_storage_uri: str | None = None

    # ---------- Email (SMTP) ----------
    # Development: Mailpit catches everything (http://localhost:8025), nothing reaches real inboxes.
    # Real delivery: point these at a real SMTP service (Gmail app password, Brevo, Resend, SES...).
    email_enabled: bool = True
    smtp_host: str = "mailpit"
    smtp_port: int = 1025
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = False  # port 587 services
    smtp_ssl: bool = False       # port 465 services
    mail_from: str = "MeetMind <no-reply@meetmind.local>"

    # ---------- AI providers (open-source models) ----------
    # Each provider is used only if configured. Order of use: NVIDIA -> Groq -> local Ollama.
    # Model IDs are config, not code: check them against each provider's /models list.
    nvidia_api_key: str = ""
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    nvidia_analysis_model: str = "nvidia/nemotron-3.5-super-vl-120b-a12b"
    nvidia_chat_model: str = "nvidia/nemotron-3.5-lightning-30b-a3b"

    groq_api_key: str = ""
    groq_analysis_model: str = "openai/gpt-oss-120b"
    groq_chat_model: str = "openai/gpt-oss-20b"
    groq_transcription_model: str = "whisper-large-v3"

    # Optional local fallback, e.g. OLLAMA_MODEL=gemma4:latest (empty = disabled)
    ollama_base_url: str = "http://host.docker.internal:11434"
    ollama_model: str = ""
    ollama_num_ctx: int = 16384

    ai_timeout_seconds: int = 180
    transcription_chunk_minutes: int = 10

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    def check_secure(self) -> None:
        """Fail fast on unsafe configuration instead of running insecurely."""
        if not self.secret_key or self.secret_key.startswith("change-me") or len(self.secret_key) < 32:
            raise RuntimeError("SECRET_KEY must be set to a random string of at least 32 characters")
        if not self.database_url:
            raise RuntimeError("DATABASE_URL must be set")
