"""
app/config.py — Bot configuration loaded from environment / .env.

No hardcoded secrets: the bot token, broker API key, and broker URL all come
from the environment. The bot shares the repo's single ``.env`` file; unknown
keys are ignored (``extra="ignore"``) so broker-only settings don't break it.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class BotSettings(BaseSettings):
  """Telegram bot settings."""

  model_config = SettingsConfigDict(
    env_file=".env",
    env_file_encoding="utf-8",
    extra="ignore",
  )

  # Telegram — shared with the broker's notification bot. Only this service
  # polls (getUpdates); the broker only sends, so there is no 409 conflict.
  TELEGRAM_BOT_TOKEN: str

  # Broker HTTP API the bot talks to.
  BROKER_API_KEY: str
  BROKER_API_PREFIX: str = ""  # secret URL segment, if the broker uses one
  BOT_BROKER_BASE_URL: str = "http://broker:8080"

  # Bot behaviour.
  BOT_LOG_LEVEL: str = "INFO"
  BOT_REQUEST_TIMEOUT: float = 10.0
  BOT_TRADES_PAGE_SIZE: int = 5


settings = BotSettings()
