"""
broker/db/models.py
────────────────────
SQLAlchemy ORM models for the audit table.

signals   — every TradingView webhook signal received by the broker
"""

from __future__ import annotations

from datetime import datetime
import uuid

from sqlalchemy import Boolean, DateTime, Enum, Float, String, func, Integer
from sqlalchemy.dialects.postgresql import JSONB, UUID
from broker.schemas.webhook_schema import SignalActionEnum
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
  symbol: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
  timeframe: Mapped[str] = mapped_column(String(20), nullable=False)
  timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

  # PositionSchema columns
  action: Mapped[SignalActionEnum] = mapped_column(
    Enum(SignalActionEnum), nullable=False
  )
  price: Mapped[float] = mapped_column(Float, nullable=False)
  quantity: Mapped[float] = mapped_column(Float, nullable=False)
  sl: Mapped[float | None] = mapped_column(Float, nullable=True)
  tp1: Mapped[float | None] = mapped_column(Float, nullable=True)
  tp2: Mapped[float | None] = mapped_column(Float, nullable=True)
  is_running: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

  # Complex objects stored as JSONB
  indicators: Mapped[dict] = mapped_column(JSONB, nullable=False)
  inputs: Mapped[dict] = mapped_column(JSONB, nullable=False)

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

  # Reference to signals
  signal_id: Mapped[uuid.UUID] = mapped_column(
    UUID(as_uuid=True), nullable=False, index=True
  )

  # Trading Account info
  account_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
  account_leverage: Mapped[int] = mapped_column(Integer, nullable=False)
  account_balance_init: Mapped[float] = mapped_column(Float, nullable=True)
  account_balance: Mapped[float] = mapped_column(Float, nullable=True)

  # Broker-specific fields
  ticket: Mapped[int | None] = mapped_column(Float, nullable=True)
  comment: Mapped[str | None] = mapped_column(String(255), nullable=True)
  magic: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

  # Trade details
  symbol: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
  action: Mapped[SignalActionEnum] = mapped_column(
    Enum(SignalActionEnum), nullable=False
  )
  price: Mapped[float] = mapped_column(Float, nullable=False)
  quantity: Mapped[float] = mapped_column(Float, nullable=False)
  sl: Mapped[float | None] = mapped_column(Float, nullable=True)
  tp1: Mapped[float | None] = mapped_column(Float, nullable=True)
  tp2: Mapped[float | None] = mapped_column(Float, nullable=True)
  is_running: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

  # Status
  status: Mapped[TradeStatusEnum] = mapped_column(Enum(TradeStatusEnum), nullable=False)

  def __repr__(self) -> str:
    return (
      f"<Trade id={self.id} signal_id={self.signal_id} "
      f"action={self.action} symbol={self.symbol}>"
    )
