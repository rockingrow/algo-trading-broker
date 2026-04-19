"""
broker/settings.py — Centralised settings loaded from .env / environment variables.
"""

from __future__ import annotations

from urllib.parse import quote
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
  model_config = SettingsConfigDict(
    env_file=".env",
    env_file_encoding="utf-8",
    extra="ignore",
  )

  # ── Webhook ──────────────────────────────────────────────────────
  WEBHOOK_HOST: str = "0.0.0.0"
  WEBHOOK_PORT: int = 80
  WEBHOOK_SECRET: str = ""  # empty = no HMAC validation

  # ── Broker API ───────────────────────────────────────────────────
  BROKER_API_KEY: str

  # ── NATS Publisher (broker → workers) ────────────────────────
  NATS_HOST: str = "localhost"   # overridden to "nats" inside Docker
  NATS_PORT: int = 4222
  NATS_TOKEN: str = ""           # token auth; leave blank = no auth

  @property
  def nats_url(self) -> str:
    return f"nats://{self.NATS_HOST}:{self.NATS_PORT}"

  # ── PostgreSQL ───────────────────────────────────────────────────
  POSTGRES_HOST: str = "localhost"
  POSTGRES_PORT: int = 5432
  POSTGRES_DB: str = "algo_trading_broker"
  POSTGRES_USER: str = "algo_trading"
  POSTGRES_PASSWORD: str = "algotrading_broker_db_password"

  # ── Logging ──────────────────────────────────────────────────────
  LOG_LEVEL: str = "INFO"

  # ── Telegram ─────────────────────────────────────────────────────
  TELEGRAM_ENABLED: bool = False
  TELEGRAM_BOT_TOKEN: str = ""
  TELEGRAM_CHAT_ID: str = ""          # management: NATS events, service start/stop
  TELEGRAM_CHAT_CHANNEL_ID: str = ""  # signals: NATS published trades

  @property
  def postgres_dsn(self) -> str:
    return (
      f"postgresql+asyncpg://{quote(self.POSTGRES_USER, safe='')}:{quote(self.POSTGRES_PASSWORD, safe='')}"
      f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
    )


settings = Settings()
