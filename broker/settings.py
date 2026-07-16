"""
broker/settings.py — Centralised settings loaded from .env / environment variables.
"""

from __future__ import annotations

from urllib.parse import quote
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
  """Application-wide configuration loaded from environment variables or a .env file."""

  model_config = SettingsConfigDict(
    env_file=".env",
    env_file_encoding="utf-8",
    extra="ignore",
  )

  # ── Webhook ──────────────────────────────────────────────────────
  WEBHOOK_HOST: str = "0.0.0.0"
  WEBHOOK_PORT: int = 80
  WEBHOOK_SECRET: str = ""  # empty = no HMAC validation
  # Uvicorn's 5s default is shorter than the gap between TradingView alerts, so
  # TradingView reuses a pooled connection the server has already closed and the
  # alert dies as "server closed the connection unexpectedly".
  WEBHOOK_KEEPALIVE_TIMEOUT: int = 120
  BROKER_PUBLIC_URL: str = (
    ""  # e.g. https://broker.example.com; falls back to host:port
  )

  @property
  def broker_url(self) -> str:
    if self.BROKER_PUBLIC_URL:
      return self.BROKER_PUBLIC_URL
    host = self.WEBHOOK_HOST if self.WEBHOOK_HOST != "0.0.0.0" else "localhost"
    return f"http://{host}:{self.WEBHOOK_PORT}"

  # ── Broker API ───────────────────────────────────────────────────
  BROKER_API_KEY: str
  BROKER_API_PREFIX: str = ""  # optional prefix for API endpoints

  # ── NATS Publisher (broker → workers) ────────────────────────
  NATS_HOST: str = "localhost"  # overridden to "nats" inside Docker
  NATS_PORT: int = 4222
  NATS_TOKEN: str = ""  # token auth; leave blank = no auth

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

  # ── API Docs (Swagger / ReDoc) ───────────────────────────────────
  # Master switch for the interactive docs. Set to ``false`` in production
  # to fully hide /docs, /redoc and /openapi.json.
  DOCS_ENABLED: bool = False

  # ── Telegram ─────────────────────────────────────────────────────
  TELEGRAM_ENABLED: bool = False
  TELEGRAM_BOT_TOKEN: str = ""
  TELEGRAM_CHAT_ID: str = ""  # management: NATS events, service start/stop
  TELEGRAM_CHAT_CHANNEL_ID: str = ""  # signals: NATS published trades

  # ── Telegram error-log hook ──────────────────────────────────────
  # When enabled (and TELEGRAM_ENABLED is true), log records at ERROR
  # level or above are forwarded to the management chat (TELEGRAM_CHAT_ID).
  TELEGRAM_LOG_ERRORS_ENABLED: bool = False
  TELEGRAM_LOG_DEDUP_WINDOW: int = 60  # seconds — suppress identical messages
  # Dedicated bot/chat that receives forwarded ERROR logs, kept separate from
  # the main bot so a Telegram outage/ban on one never affects the other.
  # Both fall back to TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID when left empty.
  TELEGRAM_LOG_CHAT_ID: str = ""
  TELEGRAM_LOG_BOT_TOKEN: str = ""

  # ── Notifications ────────────────────────────────────────────────
  # Fallback UTC offset (hours) used to render Telegram notification
  # timestamps when the `notification_timezone` broker setting is unset.
  DEFAULT_NOTIFICATION_TIMEZONE_OFFSET_HOURS: float = 7.0

  # ── Signal handler retry policy ──────────────────────────────────
  # Total attempts the JetStream handler + retry job may spend on a single
  # signal before it is marked FAILED. Persisted as the initial ``attempts``
  # value on every new ``signals`` row; on each failed fan-out the row's
  # counter is decremented and re-picked up by the retry job until it hits 0.
  # Kept here (not in ``.env.example``) because it changes retry semantics
  # rather than deployment topology.
  SIGNAL_MAX_ATTEMPTS: int = 3
  # Poll cadence (seconds) of the retry job and the minimum gap between two
  # attempts on the same row — a row whose ``last_attempt`` is newer than
  # ``now - SIGNAL_RETRY_INTERVAL_SECONDS`` is not re-picked, which stops the
  # job from racing an in-flight attempt.
  SIGNAL_RETRY_INTERVAL_SECONDS: int = 15

  @property
  def postgres_dsn(self) -> str:
    return (
      f"postgresql+asyncpg://{quote(self.POSTGRES_USER, safe='')}:{quote(self.POSTGRES_PASSWORD, safe='')}"
      f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
    )


settings = Settings()
