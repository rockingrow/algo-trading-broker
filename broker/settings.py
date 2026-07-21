"""
broker/settings.py — Centralised settings loaded from .env / environment variables.

Settings are grouped into focused ``*Settings`` sub-models nested under the main
:class:`Settings` (e.g. ``settings.webhook.HOST``, ``settings.nats.url``). Each
sub-model carries an ``env_prefix`` so the on-disk env var names are unchanged —
``WEBHOOK_HOST`` still populates ``settings.webhook.HOST`` — and can be read
standalone in isolation (handy for tests). The main ``Settings`` keeps the
cross-group convenience properties (``broker_url``, ``nats_url``,
``postgres_dsn``) so existing callers of those are untouched.
"""

from __future__ import annotations

from urllib.parse import quote

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _config(prefix: str) -> SettingsConfigDict:
  """Shared sub-model config: read the same .env, ignore unrelated vars, and
  scope this group's fields to *prefix* so env var names stay flat."""
  return SettingsConfigDict(
    env_file=".env",
    env_file_encoding="utf-8",
    extra="ignore",
    env_prefix=prefix,
  )


class WebhookSettings(BaseSettings):
  """TradingView webhook server (env prefix ``WEBHOOK_``)."""

  model_config = _config("WEBHOOK_")

  HOST: str = "0.0.0.0"
  PORT: int = 80
  SECRET: str = ""  # empty = no HMAC validation
  # Uvicorn's 5s default is shorter than the gap between TradingView alerts, so
  # TradingView reuses a pooled connection the server has already closed and the
  # alert dies as "server closed the connection unexpectedly".
  KEEPALIVE_TIMEOUT: int = 120


class BrokerApiSettings(BaseSettings):
  """Broker HTTP API (env prefix ``BROKER_``)."""

  model_config = _config("BROKER_")

  API_KEY: str  # BROKER_API_KEY — required
  API_PREFIX: str = ""  # optional prefix for API endpoints
  PUBLIC_URL: str = ""  # e.g. https://broker.example.com; falls back to host:port


class NatsSettings(BaseSettings):
  """NATS connection (broker → workers) (env prefix ``NATS_``)."""

  model_config = _config("NATS_")

  HOST: str = "localhost"  # overridden to "nats" inside Docker
  PORT: int = 4222
  TOKEN: str = ""  # token auth; leave blank = no auth

  @property
  def url(self) -> str:
    return f"nats://{self.HOST}:{self.PORT}"


class PostgresSettings(BaseSettings):
  """PostgreSQL connection (env prefix ``POSTGRES_``)."""

  model_config = _config("POSTGRES_")

  HOST: str = "localhost"
  PORT: int = 5432
  DB: str = "algo_trading_broker"
  USER: str = "algo_trading"
  PASSWORD: str = "algotrading_broker_db_password"

  @property
  def dsn(self) -> str:
    return (
      f"postgresql+asyncpg://{quote(self.USER, safe='')}:{quote(self.PASSWORD, safe='')}"
      f"@{self.HOST}:{self.PORT}/{self.DB}"
    )


class LoggingSettings(BaseSettings):
  """Application logging (env prefix ``LOG_``)."""

  model_config = _config("LOG_")

  LEVEL: str = "INFO"


class DocsSettings(BaseSettings):
  """Interactive API docs (env prefix ``DOCS_``).

  Master switch for Swagger / ReDoc. Set ``DOCS_ENABLED=false`` in production to
  fully hide /docs, /redoc and /openapi.json.
  """

  model_config = _config("DOCS_")

  ENABLED: bool = False


class TelegramSettings(BaseSettings):
  """Telegram notification + error-log channels (env prefix ``TELEGRAM_``)."""

  model_config = _config("TELEGRAM_")

  ENABLED: bool = False
  BOT_TOKEN: str = ""
  CHAT_ID: str = ""  # management: NATS events, service start/stop
  CHAT_CHANNEL_ID: str = ""  # signals: NATS published trades

  # Token of the *bot-service* BotFather bot (the one end-users actually DM to
  # link and drive their account — BOT_TELEGRAM_TOKEN in the bot's config).
  # Read from the shared .env so the broker can DM an account's owner directly
  # (completed-trade broadcasts): a user can only be messaged by the bot they
  # started, and that is this bot, not the broker's own notification bot
  # (BOT_TOKEN). Empty disables owner broadcasts. Its env var breaks the
  # ``TELEGRAM_`` prefix, so it is pinned with an explicit alias.
  SERVICE_BOT_TOKEN: str = Field(default="", validation_alias="BOT_TELEGRAM_TOKEN")

  # Error-log hook: when enabled (and ENABLED is true), log records at ERROR
  # level or above are forwarded to the management chat (CHAT_ID).
  LOG_ERRORS_ENABLED: bool = False
  LOG_DEDUP_WINDOW: int = 60  # seconds — suppress identical messages
  # Dedicated bot/chat that receives forwarded ERROR logs, kept separate from
  # the main bot so a Telegram outage/ban on one never affects the other.
  # Both fall back to BOT_TOKEN / CHAT_ID when left empty.
  LOG_CHAT_ID: str = ""
  LOG_BOT_TOKEN: str = ""


class NotificationSettings(BaseSettings):
  """Notification rendering (env prefix ``NOTIFICATION_``)."""

  model_config = _config("NOTIFICATION_")

  # Fallback UTC offset (hours) used to render Telegram notification timestamps
  # when the ``notification_timezone`` broker setting is unset. Its env var
  # (``DEFAULT_NOTIFICATION_TIMEZONE_OFFSET_HOURS``) predates the prefix, so it
  # is pinned with an explicit alias.
  DEFAULT_TIMEZONE_OFFSET_HOURS: float = Field(
    default=7.0, validation_alias="DEFAULT_NOTIFICATION_TIMEZONE_OFFSET_HOURS"
  )


class SignalSettings(BaseSettings):
  """Signal handler retry policy (env prefix ``SIGNAL_``)."""

  model_config = _config("SIGNAL_")

  # Total attempts the JetStream handler + retry job may spend on a single
  # signal before it is marked FAILED. Persisted as the initial ``attempts``
  # value on every new ``signals`` row; on each failed fan-out the row's
  # counter is decremented and re-picked up by the retry job until it hits 0.
  MAX_ATTEMPTS: int = 3
  # Poll cadence (seconds) of the retry job and the minimum gap between two
  # attempts on the same row — a row whose ``last_attempt`` is newer than
  # ``now - RETRY_INTERVAL_SECONDS`` is not re-picked, which stops the job from
  # racing an in-flight attempt.
  RETRY_INTERVAL_SECONDS: int = 15


class JetStreamSettings(BaseSettings):
  """JetStream consumer that feeds ``handle_enqueued`` (env prefix ``JETSTREAM_``)."""

  model_config = _config("JETSTREAM_")

  # Consumer name is durable so a broker restart resumes from the same position
  # on the stream instead of skipping past unacked messages.
  SIGNAL_CONSUMER: str = "broker_signal_handler"
  # Batch size and fetch timeout picked for a single-writer webhook: even the
  # noisiest TradingView setup rarely bursts more than a handful of alerts a
  # second, so 10-per-fetch keeps latency low while amortising the pull round trip.
  FETCH_BATCH: int = 10
  FETCH_TIMEOUT_SECONDS: float = 1.0


class Settings(BaseSettings):
  """Application-wide configuration, grouped into nested ``*Settings`` models."""

  model_config = SettingsConfigDict(
    env_file=".env",
    env_file_encoding="utf-8",
    extra="ignore",
  )

  webhook: WebhookSettings = Field(default_factory=WebhookSettings)
  broker_api: BrokerApiSettings = Field(default_factory=BrokerApiSettings)
  nats: NatsSettings = Field(default_factory=NatsSettings)
  postgres: PostgresSettings = Field(default_factory=PostgresSettings)
  logging: LoggingSettings = Field(default_factory=LoggingSettings)
  docs: DocsSettings = Field(default_factory=DocsSettings)
  telegram: TelegramSettings = Field(default_factory=TelegramSettings)
  notification: NotificationSettings = Field(default_factory=NotificationSettings)
  signal: SignalSettings = Field(default_factory=SignalSettings)
  jetstream: JetStreamSettings = Field(default_factory=JetStreamSettings)

  # ── Cross-group convenience properties (kept flat for existing callers) ──

  @property
  def broker_url(self) -> str:
    if self.broker_api.PUBLIC_URL:
      return self.broker_api.PUBLIC_URL
    host = self.webhook.HOST if self.webhook.HOST != "0.0.0.0" else "localhost"
    return f"http://{host}:{self.webhook.PORT}"

  @property
  def nats_url(self) -> str:
    return self.nats.url

  @property
  def postgres_dsn(self) -> str:
    return self.postgres.dsn


settings = Settings()
