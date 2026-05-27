"""Centralized configuration loaded from environment variables and .env files.

All runtime config flows through `Settings`. Modules import `get_settings()`,
never read `os.environ` directly. This is what lets us swap config in tests.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """All runtime configuration."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Data sources ---
    finnhub_api_key: str = Field(default="", description="Finnhub API key (free tier)")
    tradier_api_key: str = Field(default="", description="Tradier sandbox key")
    tradier_base_url: str = Field(default="https://sandbox.tradier.com/v1")
    sec_user_agent: str = Field(
        default="Catalyst Engine research@example.com",
        description="SEC requires a User-Agent identifying the requester",
    )

    # --- Storage ---
    duckdb_path: Path = Field(default=PROJECT_ROOT / "data" / "processed" / "catalyst.duckdb")
    raw_data_dir: Path = Field(default=PROJECT_ROOT / "data" / "raw")
    interim_data_dir: Path = Field(default=PROJECT_ROOT / "data" / "interim")

    # --- Runtime ---
    log_level: str = Field(default="INFO")
    environment: str = Field(default="local", description="local | ci | prod")

    # --- Alerts ---
    telegram_bot_token: str = Field(default="")
    telegram_chat_id: str = Field(default="")

    @property
    def project_root(self) -> Path:
        return PROJECT_ROOT


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the singleton Settings instance.

    Cached so we parse .env exactly once per process. Tests can clear the
    cache with `get_settings.cache_clear()`.
    """
    return Settings()
