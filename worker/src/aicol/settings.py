"""Worker configuration, read from the environment (and `worker/.env` when present)."""

from __future__ import annotations

import socket

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = Field(alias="DATABASE_URL")
    openrouter_api_key: str | None = Field(default=None, alias="OPENROUTER_API_KEY")
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1", alias="OPENROUTER_BASE_URL"
    )
    worker_id: str = Field(default_factory=lambda: socket.gethostname(), alias="AICOL_WORKER_ID")
    poll_interval_seconds: float = Field(default=5.0, alias="AICOL_POLL_INTERVAL_SECONDS")
    claim_timeout_seconds: int = Field(default=300, alias="AICOL_CLAIM_TIMEOUT_SECONDS")
    maintenance_interval_seconds: float = Field(
        default=3600.0, gt=0, alias="AICOL_MAINTENANCE_INTERVAL_SECONDS"
    )
    log_level: str = Field(default="info", alias="AICOL_LOG_LEVEL")


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
