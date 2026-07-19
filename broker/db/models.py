"""
broker/db/models.py
────────────────────
SQLAlchemy ORM models for the audit table.

signals   — every TradingView webhook signal received by the broker
"""

from __future__ import annotations

from datetime import datetime
import uuid

from sqlalchemy import (
  Boolean,
  DateTime,
  Enum,
  ForeignKey,
  Numeric,
  String,
  Text,
  func,
  Integer,
  UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from broker.schemas.account_schema import MarketTypeEnum
from broker.schemas.core import (
  BotPlatformTypeEnum,
  SignalActionEnum,
  SignalStatusEnum,
)
from broker.schemas.trade_schema import TradeStatusEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
  """Abstract base for all ORM models, providing UUID primary key and auto-managed timestamps."""

  id: Mapped[uuid.UUID] = mapped_column(
    UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
  )
  createdAt: Mapped[datetime] = mapped_column(
    DateTime(timezone=True),
    nullable=False,
    server_default=func.now(),
  )
  updatedAt: Mapped[datetime] = mapped_column(
    DateTime(timezone=True),
    nullable=False,
    server_default=func.now(),
    onupdate=func.now(),
  )


class Signal(Base):
  """
  One row per incoming TradingView webhook signal.
  Logs components of the WebhookPayload.
  """

  __tablename__ = "signals"

  # WebhookPayload columns
  strategy: Mapped[str] = mapped_column(String(50), nullable=False)
  symbol: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
  timeframe: Mapped[str] = mapped_column(String(20), nullable=False)
  timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

  # PositionSchema columns
  action: Mapped[SignalActionEnum] = mapped_column(
    Enum(SignalActionEnum), nullable=False
  )
  price: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)
  quantity: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)
  sl: Mapped[float | None] = mapped_column(Numeric(20, 8), nullable=True)
  tp1: Mapped[float | None] = mapped_column(Numeric(20, 8), nullable=True)
  tp2: Mapped[float | None] = mapped_column(Numeric(20, 8), nullable=True)
  is_running: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
  risk_percent: Mapped[float] = mapped_column(
    Numeric(10, 4), nullable=False, default=0.0
  )
  is_scale_position: Mapped[bool] = mapped_column(
    Boolean, nullable=False, default=False
  )
  scale_strategy: Mapped[str | None] = mapped_column(String(50), nullable=True)

  # Delivery state: QUEUED once persisted, PUBLISHED after the JetStream handler
  # has fanned it out to workers and finished the notification pipeline, or
  # FAILED once every retry attempt has been exhausted. Rows stay QUEUED
  # between attempts so the 15s retry job can re-pick them.
  status: Mapped[SignalStatusEnum] = mapped_column(
    Enum(SignalStatusEnum),
    nullable=False,
    default=SignalStatusEnum.QUEUED,
    server_default=SignalStatusEnum.QUEUED.value,
    index=True,
  )
  # Remaining fan-out attempts. Seeded from ``settings.SIGNAL_MAX_ATTEMPTS``
  # on insert and decremented on every failed attempt; when it would drop to
  # ``0`` the row is flipped to ``FAILED`` instead.
  attempts: Mapped[int] = mapped_column(
    Integer,
    nullable=False,
    default=0,
    server_default="0",
  )
  # Timestamp of the last attempt (nullable — a QUEUED row that has never been
  # attempted yet has ``NULL`` here). Used by the retry job to enforce the
  # minimum gap between two attempts on the same row so a poll tick cannot
  # race an in-flight attempt.
  last_attempt: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True), nullable=True
  )

  # Complex objects stored as JSONB
  indicators: Mapped[dict] = mapped_column(JSONB, nullable=True)
  inputs: Mapped[dict] = mapped_column(JSONB, nullable=True)
  raw: Mapped[dict] = mapped_column(JSONB, nullable=True)

  def __repr__(self) -> str:
    return (
      f"<Signal id={self.id} symbol={self.symbol} "
      f"action={self.action} timestamp={self.timestamp}>"
    )


class Trade(Base):
  """
  One row per trade opened by the broker.
  """

  __tablename__ = "trades"
  __table_args__ = (
    UniqueConstraint(
      "market",
      "gateway",
      "account_id",
      "ref_id",
      name="uq_trades_market_gateway_account_ref",
    ),
  )

  # Trading Account info
  account_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
  # Denormalized from the owning accounts row at upsert time (see
  # TradeRepository._upsert_account) — account_id alone doesn't identify an
  # account uniquely, so these two are needed to scope trades to the right one.
  market: Mapped[MarketTypeEnum | None] = mapped_column(
    Enum(MarketTypeEnum), nullable=True
  )
  gateway: Mapped[str | None] = mapped_column(String(50), nullable=True)
  account_leverage: Mapped[int | None] = mapped_column(Integer, nullable=True)
  account_balance_init: Mapped[float] = mapped_column(Numeric(20, 8), nullable=True)
  account_balance: Mapped[float] = mapped_column(Numeric(20, 8), nullable=True)

  # Strategy
  strategy: Mapped[str] = mapped_column(String(50), nullable=False)
  strategy_code: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

  # Trade
  ref_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

  # Trade details
  symbol: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
  action: Mapped[SignalActionEnum] = mapped_column(
    Enum(SignalActionEnum), nullable=False
  )
  price: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)
  quantity: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)
  sl: Mapped[float | None] = mapped_column(Numeric(20, 8), nullable=True)
  tp1: Mapped[float | None] = mapped_column(Numeric(20, 8), nullable=True)
  tp2: Mapped[float | None] = mapped_column(Numeric(20, 8), nullable=True)
  is_running: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
  risk_percent: Mapped[float] = mapped_column(
    Numeric(10, 4), nullable=False, default=0.0
  )
  comment: Mapped[str | None] = mapped_column(String(255), nullable=True)
  gateway_return_code: Mapped[int | None] = mapped_column(Integer, nullable=True)

  # Status
  status: Mapped[TradeStatusEnum] = mapped_column(Enum(TradeStatusEnum), nullable=False)
  reject_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

  def __repr__(self) -> str:
    return (
      f"<Trade id={self.id} account_id={self.account_id} gateway={self.gateway} "
      f"ref_id={self.ref_id} action={self.action} symbol={self.symbol}>"
    )


class Account(Base):
  """
  One row per account managed by the broker.
  """

  __tablename__ = "accounts"
  # account_id alone is NOT unique: two different real accounts on different
  # gateways (e.g. an MT5 login and a Binance account) can coincidentally
  # share the same bare id. The composite key is what's actually unique.
  __table_args__ = (
    UniqueConstraint(
      "market", "gateway", "account_id", name="uq_accounts_market_gateway_account_id"
    ),
  )

  # Trading Account info
  account_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
  account_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
  account_balance: Mapped[float] = mapped_column(Numeric(20, 8), nullable=True)
  market: Mapped[MarketTypeEnum] = mapped_column(
    Enum(MarketTypeEnum), nullable=False
  )
  # Exchange/gateway the account trades through, e.g. MT5 (forex) or BINANCE
  # (crypto). Combined with market + account_id it forms the worker
  # addressing id <market>-<gateway>-<account_id> used on the SYSTEM subject.
  # Nullable so rows predating this column (or workers that don't report it)
  # remain valid.
  gateway: Mapped[str | None] = mapped_column(String(50), nullable=True)

  last_activity_at: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True), nullable=True
  )

  # No bot/chat-platform columns live here on purpose: an account is a trading
  # domain object and must not know about Telegram. Who may drive it from a
  # bot lives in ``AccountBotLink``, the invite secrets in
  # ``AccountLinkToken``, and the per-user active selection in ``BotSession``.

  def __repr__(self) -> str:
    return (
      f"<Account id={self.id} account_id={self.account_id} "
      f"market={self.market} gateway={self.gateway}>"
    )


class AccountBotLink(Base):
  """
  Many-to-many join between trading accounts and chat-platform users.

  One account may be driven by several bot users (e.g. an owner plus an
  assistant), and one bot user may hold several accounts (different
  market/gateway pairs) — neither direction is expressible as a column on
  ``accounts``, which is why this table exists.

  There is deliberately no role/permission column yet: every linked user has
  the same rights. Add one here when a real read-only use case shows up.

  ``platform_user_id`` is a *string*, not an integer, even though Telegram and
  Discord ids are 64-bit numbers — Slack/Matrix ids are opaque strings, so
  storing text avoids a second migration later. The API layer still speaks
  ``telegram_user_id: int``; the repository is the single place that casts.
  """

  __tablename__ = "account_bot_links"
  __table_args__ = (
    UniqueConstraint(
      "platform",
      "platform_user_id",
      "account_id",
      name="uq_account_bot_links_platform_user_account",
    ),
  )

  account_id: Mapped[uuid.UUID] = mapped_column(
    UUID(as_uuid=True),
    ForeignKey("accounts.id", ondelete="CASCADE"),
    nullable=False,
    index=True,
  )
  platform: Mapped[BotPlatformTypeEnum] = mapped_column(
    Enum(BotPlatformTypeEnum), nullable=False
  )
  platform_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

  def __repr__(self) -> str:
    return (
      f"<AccountBotLink account_id={self.account_id} "
      f"platform={self.platform} platform_user_id={self.platform_user_id}>"
    )


class AccountLinkToken(Base):
  """
  Invite secrets that let a bot user claim an account.

  Split out of ``accounts`` so an account can have several outstanding tokens
  (invite two people with two separately revocable secrets) and so revocation
  is a state change rather than an overwrite.

  A token is *valid* when ``revoked_at IS NULL`` and it has not expired.
  ``expires_at`` is NULL by default, meaning "never expires" — nothing issues
  a deadline today; the column exists so time-boxed invites need no migration.
  ``last_used_at`` is audit only and never affects validity: a token stays
  reusable after a successful link, matching the pre-existing behaviour.
  """

  __tablename__ = "account_link_tokens"

  account_id: Mapped[uuid.UUID] = mapped_column(
    UUID(as_uuid=True),
    ForeignKey("accounts.id", ondelete="CASCADE"),
    nullable=False,
    index=True,
  )
  token: Mapped[uuid.UUID] = mapped_column(
    UUID(as_uuid=True), nullable=False, unique=True, index=True, default=uuid.uuid4
  )
  expires_at: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True), nullable=True
  )
  revoked_at: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True), nullable=True
  )
  last_used_at: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True), nullable=True
  )

  def __repr__(self) -> str:
    return (
      f"<AccountLinkToken account_id={self.account_id} "
      f"revoked_at={self.revoked_at} expires_at={self.expires_at}>"
    )


class BotSession(Base):
  """
  One row per (platform, bot user), tracking which of their (possibly several)
  linked accounts is currently "active" — the one single-account commands
  (/status, /trades, /flat, ...) act on. Kept separate from
  ``AccountBotLink`` because "may drive" and "is currently driving" are
  different facts: a user has N links but exactly one active selection.
  """

  __tablename__ = "bot_sessions"
  __table_args__ = (
    UniqueConstraint(
      "platform", "platform_user_id", name="uq_bot_sessions_platform_user"
    ),
  )

  platform: Mapped[BotPlatformTypeEnum] = mapped_column(
    Enum(BotPlatformTypeEnum), nullable=False
  )
  platform_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
  active_account_id: Mapped[uuid.UUID | None] = mapped_column(
    UUID(as_uuid=True),
    ForeignKey("accounts.id", ondelete="SET NULL"),
    nullable=True,
  )

  def __repr__(self) -> str:
    return (
      f"<BotSession platform={self.platform} "
      f"platform_user_id={self.platform_user_id} "
      f"active_account_id={self.active_account_id}>"
    )


class BrokerSetting(Base):
  """
  One row per broker setting.
  """

  __tablename__ = "broker_settings"
  __table_args__ = (UniqueConstraint("key", name="uq_broker_settings_key"),)

  key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
  value: Mapped[str] = mapped_column(Text, nullable=False)

  def __repr__(self) -> str:
    return f"<BrokerSetting id={self.id} key={self.key} value={self.value}>"
