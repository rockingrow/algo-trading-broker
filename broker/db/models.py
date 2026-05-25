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
  BigInteger,
  Boolean,
  DateTime,
  Enum,
  Numeric,
  String,
  func,
  Integer,
  UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from broker.schemas.account_schema import MarketTypeEnum
from broker.schemas.core import SignalActionEnum
from broker.schemas.trade_schema import TradeStatusEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
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
  risk_percent: Mapped[float] = mapped_column(Numeric(10, 4), nullable=False, default=0.0)

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
    UniqueConstraint("account_id", "ticket", name="uq_trades_account_ticket"),
  )

  # Trading Account info
  account_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
  account_leverage: Mapped[int] = mapped_column(Integer, nullable=False)
  account_balance_init: Mapped[float] = mapped_column(Numeric(20, 8), nullable=True)
  account_balance: Mapped[float] = mapped_column(Numeric(20, 8), nullable=True)

  # Broker-specific fields
  ticket: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
  comment: Mapped[str | None] = mapped_column(String(255), nullable=True)
  magic: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

  # Trade details
  strategy: Mapped[str] = mapped_column(String(50), nullable=False)
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
  risk_percent: Mapped[float] = mapped_column(Numeric(10, 4), nullable=False, default=0.0)

  # Status
  status: Mapped[TradeStatusEnum] = mapped_column(Enum(TradeStatusEnum), nullable=False)
  reject_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

  def __repr__(self) -> str:
    return (
      f"<Trade id={self.id} account_id={self.account_id} ticket={self.ticket} "
      f"action={self.action} symbol={self.symbol}>"
    )


class Account(Base):
  """
  One row per account managed by the broker.
  """

  __tablename__ = "accounts"
  __table_args__ = (UniqueConstraint("account_id", name="uq_accounts_account_id"),)

  # Trading Account info
  account_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
  account_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
  account_balance: Mapped[float] = mapped_column(Numeric(20, 8), nullable=True)
  market_type: Mapped[MarketTypeEnum] = mapped_column(
    Enum(MarketTypeEnum), nullable=False
  )

  last_activity_at: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True), nullable=True
  )

  def __repr__(self) -> str:
    return f"<Account id={self.id} account_id={self.account_id} market_type={self.market_type}>"


class BrokerSetting(Base):
  """
  One row per broker setting.
  """

  __tablename__ = "broker_settings"
  __table_args__ = (UniqueConstraint("key", name="uq_broker_settings_key"),)

  key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
  value: Mapped[str] = mapped_column(String(255), nullable=False)

  def __repr__(self) -> str:
    return f"<BrokerSetting id={self.id} key={self.key} value={self.value}>"
