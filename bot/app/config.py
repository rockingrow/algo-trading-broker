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
    # Repo shares one .env at the root; when running the bot locally from ./bot
    # that file is one level up. Later files win, so a bot-local .env overrides.
    env_file=("../.env", ".env"),
    env_file_encoding="utf-8",
    extra="ignore",
  )

  # Telegram — this bot is a separate BotFather bot from the broker's
  # notification bot (TELEGRAM_BOT_TOKEN in broker/settings.py), so it can
  # poll (getUpdates) without any risk of a 409 conflict with broker sends.
  BOT_TELEGRAM_TOKEN: str

  # Comma-separated Telegram user IDs granted admin commands (e.g. "123,456").
  TELEGRAM_ADMIN_IDS: str = ""

  # Broker HTTP API the bot talks to.
  BROKER_API_KEY: str
  BROKER_API_PREFIX: str = ""  # secret URL segment, if the broker uses one
  BOT_BROKER_BASE_URL: str = "http://broker:8080"

  # Bot behaviour.
  # Page sizes are not here: they're sized to each table's width, so they live
  # as code constants in app/constants.py rather than as deployment knobs.
  BOT_LOG_LEVEL: str = "INFO"
  BOT_REQUEST_TIMEOUT: float = 10.0

  @property
  def admin_ids(self) -> set[int]:
    """Parse TELEGRAM_ADMIN_IDS into a set of ints (ignores blanks/non-numbers)."""
    ids: set[int] = set()
    for part in self.TELEGRAM_ADMIN_IDS.split(","):
      part = part.strip()
      if part.isdigit():
        ids.add(int(part))
    return ids


settings = BotSettings()
