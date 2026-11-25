from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = f"sqlite:///{(BASE_DIR / 'data' / 'ops_agent.db').as_posix()}"
    output_dir: Path = BASE_DIR / "outputs"

    underperformance_threshold: float = Field(default=0.80, gt=0, le=1.5)
    severe_underperformance_threshold: float = Field(default=0.60, gt=0, le=1.5)
    task_due_days_after_period: int = Field(default=7, ge=0, le=60)

    max_retries: int = Field(default=3, ge=1, le=10)
    retry_base_delay_seconds: float = Field(default=0.5, ge=0)
    max_request_chars: int = Field(default=2000, ge=50)

    llm_provider: str = "deterministic"  # deterministic | openai | gemini
    llm_timeout_seconds: float = Field(default=20.0, gt=0)
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-1.5-flash"

    upload_dir: Path = BASE_DIR / "data" / "uploads"
    max_upload_mb: int = Field(default=10, ge=1, le=100)
    chunk_max_chars: int = Field(default=600, ge=200, le=4000)
    chunk_overlap_chars: int = Field(default=120, ge=0, le=1000)
    min_relevance_score: float = Field(default=0.05, ge=0, le=1)

    email_provider: str = "simulated"  # simulated | smtp
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_timeout_seconds: float = Field(default=20.0, gt=0)

    leave_low_balance_days: float = Field(default=3.0, ge=0)
    leave_upcoming_days: int = Field(default=7, ge=1, le=60)


@lru_cache
def get_settings() -> Settings:
    return Settings()
