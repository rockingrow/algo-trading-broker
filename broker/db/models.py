"""
broker/db/models.py
────────────────────
SQLAlchemy ORM models for the audit table.

signal_log   — every TradingView webhook signal received by the broker
"""

from __future__ import annotations

from datetime import datetime
import uuid

from sqlalchemy import (
  Boolean,
  DateTime,
  Enum,
  Float,
  String,
  func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from broker.schemas.webhook import SignalActionEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
  pass


class SignalLog(Base):
  """
  One row per incoming TradingView webhook signal.
  Logs components of the WebhookPayload.
  """

  __tablename__ = "signal_log"

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
      f"<SignalLog id={self.id} symbol={self.symbol} "
      f"action={self.action} timestamp={self.timestamp}>"
    )
