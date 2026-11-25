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

    # ---------- Reading slides (vision models) ----------
    # Tried in this order: Groq (fastest, many slides per meeting) -> NVIDIA -> local Ollama. Empty = skip that one.
    groq_vision_model: str = "qwen/qwen3.8-27b"
    nvidia_vision_model: str = "nvidia/nemotron-3.5-super-vl-120b-a12b"
    ollama_vision_model: str = ""       # empty = use OLLAMA_MODEL (gemma4 can read images)
    max_slides_per_recording: int = 60  # slides kept from one screen recording
    # Slides are found as periods where the screen stands still; this "picture change" threshold (0-1) is
    # only the fallback for recordings that never stand still. Same-template slides differ by only ~0.05.
    slide_scene_threshold: float = 0.03
    slide_min_gap_seconds: int = 4      # ignore changes closer together than this (animations, scrolling)
    max_slide_image_mb: int = 10        # photos of the big screen

    # ---------- Live recording, documents, reminders ----------
    max_live_recording_mb: int = 4000   # recordings made in the browser (video can be large)
    live_chunk_max_mb: int = 25         # one piece of a live recording (sent every few seconds)
    max_document_mb: int = 25
    asap_working_days: int = 2          # "as soon as possible" = this many working days, marked urgent
    reminder_hour: int = 9              # reminder emails go out at this hour...
    reminder_timezone: str = "UTC"      # ...in this time zone (e.g. Asia/Kolkata)

    # ---------- Agents ----------
    agent_max_steps: int = 8            # tool calls an assistant may make for one question

    # ---------- RAG (Phase 6) ----------
    # Vector database. Empty CHROMA_URL disables semantic search (keyword search is used instead).
    chroma_url: str = "http://chroma:8000"
    # nvidia | ollama. Vectors from different models can't be mixed, so each model gets its own collection.
    embedding_provider: str = "nvidia"
    nvidia_embedding_model: str = "nvidia/nemotron-3.5-embed-1b"
    ollama_embedding_model: str = "embeddinggemma:latest"
    rag_chunk_chars: int = 1200      # ~1 minute of speech per chunk
    rag_top_k: int = 8               # chunks given to the model per answer
    rag_full_context_chars: int = 40_000  # per-meeting Q&A sends the whole meeting below this size
    rag_rerank: bool = True          # a fast LLM re-orders search results by relevance (no reranker model needed)

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
