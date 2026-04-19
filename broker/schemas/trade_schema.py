"""
broker/schemas/trade_schema.py
──────────────────────────────
Pydantic schemas for Trade API endpoints.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from broker.schemas.core import SignalActionEnum


class TradeStatusEnum(str, Enum):
  OPENED = "OPENED"
  REJECTED = "REJECTED"
  PARTIALLY_CLOSED = "PARTIALLY_CLOSED"
  CLOSED = "CLOSED"
  FLAT = "FLAT"


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class TradeCreateRequest(BaseModel):
  """Payload sent by the worker node to create a Trade record."""

  # Account info
  account_id: str = Field(..., max_length=50, description="Broker account identifier")
  account_leverage: int = Field(..., description="Account leverage (e.g. 100)")
  account_balance_init: Optional[float] = Field(
    None, description="Account balance before trade"
  )
  account_balance: Optional[float] = Field(
    None, description="Account balance after trade open"
  )

  # Broker-specific
  ticket: Optional[float] = Field(None, description="Broker order ticket / deal ID")
  comment: Optional[str] = Field(
    None, max_length=255, description="Broker comment string"
  )
  magic: str = Field(
    ..., max_length=50, description="EA magic number / strategy identifier"
  )

  # Trade details
  strategy: str = Field(..., max_length=50, description="Strategy name, e.g. MT5_GOLD_M5")
  symbol: str = Field(..., max_length=50, description="Trading instrument symbol")
  action: SignalActionEnum = Field(..., description="Trade direction")
  price: float = Field(..., description="Execution price")
  quantity: float = Field(..., description="Lot size / quantity")
  sl: Optional[float] = Field(None, description="Stop-loss price")
  tp1: Optional[float] = Field(None, description="Take-profit 1 price")
  tp2: Optional[float] = Field(None, description="Take-profit 2 price")
  is_running: bool = Field(False, description="Whether the trade is currently active")
  risk_percent: float = Field(0.0, description="Risk as percent of account balance")

  # Status
  status: TradeStatusEnum = Field(..., description="Initial trade status")
  reject_reason: Optional[str] = Field(
    None, max_length=255, description="Reason for rejection if status=REJECTED"
  )


class TradeUpdateRequest(BaseModel):
  """Payload sent by the worker node to update an existing Trade record."""

  # Account info (may change after partial close etc.)
  account_leverage: Optional[int] = Field(None, description="Account leverage")
  account_balance_init: Optional[float] = Field(
    None, description="Account balance snapshot at time of update"
  )
  account_balance: Optional[float] = Field(None, description="Updated account balance")

  # Broker-specific
  ticket: Optional[float] = Field(
    None, description="Broker order ticket (if assigned after open)"
  )
  comment: Optional[str] = Field(
    None, max_length=255, description="Updated broker comment"
  )

  # Trade identity (informational — confirms which trade is being updated)
  strategy: Optional[str] = Field(None, max_length=50, description="Strategy name")
  symbol: Optional[str] = Field(None, max_length=50, description="Trading instrument symbol")

  # Trade details
  price: Optional[float] = Field(None, description="Updated execution/close price")
  quantity: Optional[float] = Field(None, description="Updated quantity")
  sl: Optional[float] = Field(None, description="Updated stop-loss price")
  tp1: Optional[float] = Field(None, description="Updated take-profit 1 price")
  tp2: Optional[float] = Field(None, description="Updated take-profit 2 price")
  is_running: Optional[bool] = Field(
    None, description="Whether the trade is still active"
  )

  # Status
  status: Optional[TradeStatusEnum] = Field(None, description="Updated trade status")
  reject_reason: Optional[str] = Field(
    None, max_length=255, description="Updated rejection reason"
  )


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class TradeResponse(BaseModel):
  """Serialised Trade row returned by the API."""

  id: uuid.UUID

  # Account info
  account_id: str
  account_leverage: int
  account_balance_init: Optional[float]
  account_balance: Optional[float]

  # Broker-specific
  ticket: Optional[float]
  comment: Optional[str]
  magic: str

  # Trade details
  strategy: str
  symbol: str
  action: SignalActionEnum
  price: float
  quantity: float
  sl: Optional[float]
  tp1: Optional[float]
  tp2: Optional[float]
  is_running: bool
  risk_percent: float

  # Status
  status: TradeStatusEnum
  reject_reason: Optional[str]

  # Timestamps
  createdAt: datetime
  updatedAt: datetime

  model_config = {"from_attributes": True}
