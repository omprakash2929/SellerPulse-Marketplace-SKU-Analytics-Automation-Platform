"""Configuration management for SellerPulse using Pydantic Settings."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables or .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Core Environment
    ENVIRONMENT: Literal["development", "staging", "production", "test"] = "development"
    LOG_LEVEL: str = "INFO"

    # Database
    DATABASE_URL: str = "sqlite:///./data/sellerpulse.db"

    # API Security & Server
    API_KEY: str = "sk_live_sellerpulse_demo_key_99"
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000

    # Dashboard
    DASHBOARD_PORT: int = 8501

    # Analytics Thresholds
    DEAD_STOCK_DAYS: int = 90
    SLOW_STOCK_DAYS: int = 60
    LOW_STOCK_DAYS_THRESHOLD: int = 14
    MARGIN_FLOOR_PCT: float = 0.15

    # Automation & Notifications
    ALERTS_WEBHOOK_ENABLED: bool = False
    ALERTS_WEBHOOK_URL: str | None = None
    ALERTS_TELEGRAM_BOT_TOKEN: str | None = None
    ALERTS_TELEGRAM_CHAT_ID: str | None = None

    # Scheduling & Output
    ETL_SYNC_INTERVAL_HOURS: int = 24
    ALERTS_CHECK_INTERVAL_HOURS: int = 1
    REPORTS_DIR: Path = Field(default_factory=lambda: Path("./reports_out"))

    def resolve_db_path(self) -> Path | None:
        """Extract and ensure parent directory exists if using SQLite."""
        if self.DATABASE_URL.startswith("sqlite:///"):
            clean_path = self.DATABASE_URL.replace("sqlite:///", "")
            if clean_path and clean_path != ":memory:":
                p = Path(clean_path).resolve()
                p.parent.mkdir(parents=True, exist_ok=True)
                return p
        return None


@lru_cache
def get_settings() -> Settings:
    """Return cached instance of application settings."""
    settings = Settings()
    settings.resolve_db_path()
    settings.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    return settings
