"""
config.py
---------
Central configuration module. Loads all settings from environment variables
or a .env file. Uses pydantic-settings for type-safe validation.

Environment variables are defined in .env (see .env.example below).
"""

from pathlib import Path
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_BASE_DIR = Path(__file__).resolve().parent
_ENV_FILE = _BASE_DIR / ".env"


class Settings(BaseSettings):
    """
    Application-wide settings.
    All values can be overridden via environment variables or a .env file.
    """

    model_config = SettingsConfigDict(
        env_file=(str(_ENV_FILE), ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------- Telegram ----------
    telegram_bot_token: SecretStr = Field(
        default=SecretStr(""),
        description="Bot token from @BotFather",
    )

    # ---------- Queue / Concurrency ----------
    max_concurrent_jobs: int = Field(
        default=3,
        ge=1,
        le=10,
        description="How many resolutions can run in parallel (Playwright contexts).",
    )
    max_queue_depth: int = Field(
        default=20,
        ge=1,
        description="If queue exceeds this, new jobs are rejected with a RATE_LIMITED message.",
    )
    per_user_job_limit: int = Field(
        default=2,
        ge=1,
        description="Max active/pending jobs a single user can have at once.",
    )

    # ---------- Timeouts (seconds) ----------
    nav_timeout_ms: int = Field(
        default=90_000,
        description="Max time for a single navigation step (Playwright timeout in ms).",
    )
    total_resolution_timeout_s: int = Field(
        default=180,
        description="Hard cap for an entire resolution attempt (all steps combined).",
    )
    warmup_delay_ms: int = Field(
        default=2_500,
        description="Delay after root-domain warm-up navigation, to mimic human.",
    )

    # ---------- Playwright ----------
    headless: bool = Field(
        default=True,
        description="Run Chromium headless. Set False for debugging.",
    )
    proxy_url: str | None = Field(
        default=None,
        description="Optional proxy URL, e.g. http://user:pass@host:port",
    )
    storage_state_dir: str = Field(
        default="data/sessions",
        description="Directory where per-domain cookies/storage are persisted.",
    )

    # ---------- Retry / Fallback ----------
    max_retries: int = Field(
        default=1,
        ge=0,
        le=3,
        description="Additional retry attempts after the first failure.",
    )
    retry_backoff_s: float = Field(
        default=3.0,
        description="Base backoff (seconds) between retries. Multiplied by attempt number.",
    )
    enable_fallback: bool = Field(
        default=True,
        description="If True, use external bypass API when browser resolution fails.",
    )
    fallback_api_url: str | None = Field(
        default=None,
        description="Optional external bypass API base URL (e.g. bypass.vip).",
    )
    fallback_api_key: SecretStr | None = Field(
        default=None,
        description="API key for the fallback service, if required.",
    )

    # ---------- Logging ----------
    log_level: str = Field(
        default="INFO",
        description="Logging level: DEBUG, INFO, WARNING, ERROR.",
    )
    log_file: str | None = Field(
        default="logs/bot.log",
        description="Log file path. Set to None to log only to console.",
    )


# Singleton instance — import this everywhere.
settings = Settings()
