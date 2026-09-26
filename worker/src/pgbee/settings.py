"""Worker configuration, read from the environment (and `worker/.env` when present)."""

from __future__ import annotations

import socket
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = Field(alias="DATABASE_URL")
    openrouter_api_key: str | None = Field(default=None, alias="OPENROUTER_API_KEY")
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1", alias="OPENROUTER_BASE_URL"
    )
    provider: Literal["openrouter", "openai"] | None = Field(default=None, alias="PGBEE_PROVIDER")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_base_url: str | None = Field(default=None, alias="OPENAI_BASE_URL")
    worker_id: str = Field(default_factory=lambda: socket.gethostname(), alias="PGBEE_WORKER_ID")
    poll_interval_seconds: float = Field(default=5.0, alias="PGBEE_POLL_INTERVAL_SECONDS")
    claim_timeout_seconds: int = Field(default=300, alias="PGBEE_CLAIM_TIMEOUT_SECONDS")
    request_timeout_seconds: float = Field(
        default=60.0, gt=0, alias="PGBEE_REQUEST_TIMEOUT_SECONDS"
    )
    maintenance_interval_seconds: float = Field(
        default=3600.0, gt=0, alias="PGBEE_MAINTENANCE_INTERVAL_SECONDS"
    )
    log_level: str = Field(default="info", alias="PGBEE_LOG_LEVEL")

    def provider_kind(self) -> Literal["openrouter", "openai"]:
        """Which provider the worker uses: PGBEE_PROVIDER when set, otherwise OpenRouter when its
        key is set, otherwise an OpenAI-compatible endpoint when its key or URL is set."""
        kind = self.provider
        if kind is None:
            if self.openrouter_api_key:
                kind = "openrouter"
            elif self.openai_api_key or self.openai_base_url:
                kind = "openai"
            else:
                raise ValueError(
                    "no model provider configured: set OPENROUTER_API_KEY, or OPENAI_API_KEY"
                    " and/or OPENAI_BASE_URL for an OpenAI-compatible endpoint"
                )
        if kind == "openrouter" and not self.openrouter_api_key:
            raise ValueError("PGBEE_PROVIDER=openrouter needs OPENROUTER_API_KEY")
        if kind == "openai" and not (self.openai_api_key or self.openai_base_url):
            raise ValueError("PGBEE_PROVIDER=openai needs OPENAI_API_KEY or OPENAI_BASE_URL")
        return kind


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
