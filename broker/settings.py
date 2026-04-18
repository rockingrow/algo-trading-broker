"""
broker/settings.py — Centralised settings loaded from .env / environment variables.
"""

from __future__ import annotations

from typing import Dict
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
  model_config = SettingsConfigDict(
    env_file=".env",
    env_file_encoding="utf-8",
    extra="ignore",
  )

  # ── Webhook ──────────────────────────────────────────────────────
  WEBHOOK_HOST: str = "0.0.0.0"
  WEBHOOK_PORT: int = 8080
  WEBHOOK_SECRET: str = ""  # empty = no HMAC validation

  # ── ZeroMQ Publisher (broker → workers) ──────────────────────
  ZMQ_BROKER_HOST: str = "*"  # bind to all interfaces
  ZMQ_PUB_PORT: int = 5555

  # ── ZeroMQ CURVE Authentication ──────────────────────────────
  # Generate a keypair with: python scripts/generate_curve_keypair.py
  ZMQ_CURVE_ENABLED: bool = False
  # Z85-encoded 40-character strings (output of zmq.curve_keypair())
  ZMQ_CURVE_SERVER_PUBLIC_KEY: str = ""  # share with every subscriber
  ZMQ_CURVE_SERVER_SECRET_KEY: str = ""  # keep on broker ONLY

  # ── PostgreSQL ───────────────────────────────────────────────────
  POSTGRES_HOST: str = "localhost"
  POSTGRES_PORT: int = 5432
  POSTGRES_DB: str = "algo_trading_broker"
  POSTGRES_USER: str = "algo_trading"
  POSTGRES_PASSWORD: str = "algotrading_broker_db_password"

  # Instrument map "UNIVERSAL=BROKER,..." → parsed to dict
  INSTRUMENT_MAP: str = "EURUSD=EURUSD,XAUUSD=XAUUSD,BTCUSD=BTCUSD"

  # ── Logging ──────────────────────────────────────────────────────
  LOG_LEVEL: str = "INFO"

  # ── Telegram ─────────────────────────────────────────────────────
  TELEGRAM_ENABLED: bool = False
  TELEGRAM_BOT_TOKEN: str = ""
  TELEGRAM_CHAT_ID: str = ""

  @property
  def postgres_dsn(self) -> str:
    return (
      f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
      f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
    )

  @property
  def instrument_map_dict(self) -> Dict[str, str]:
    """Parse INSTRUMENT_MAP env string into {universal: broker} dict."""
    result: Dict[str, str] = {}
    for pair in self.INSTRUMENT_MAP.split(","):
      pair = pair.strip()
      if "=" in pair:
        k, v = pair.split("=", 1)
        result[k.strip().upper()] = v.strip()
    return result


settings = Settings()
