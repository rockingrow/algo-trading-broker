"""
broker/schemas/trade_schema.py
──────────────────────────────
Status enum shared by the Trade ORM model and the NATS TRADE handler.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import List, Literal, Optional

from pydantic import BaseModel

from broker.schemas.account_schema import MarketTypeEnum
from broker.schemas.core import SignalActionEnum


class TradeStatusEnum(str, Enum):
  """Lifecycle states of a trade row, progressing from open through partial fills to close or rejection."""

  OPENED = "OPENED"
  REJECTED = "REJECTED"
  PARTIALLY_CLOSED = "PARTIALLY_CLOSED"
  CLOSED = "CLOSED"
  FLAT = "FLAT"


class TradeCard(BaseModel):
  """A live trade card already posted to one subscriber.

  The value-object view of a ``trade_notifications`` row: everything the card
  service needs to decide whether to edit that message, and nothing else. Kept
  out of the ORM layer so the service (and its tests) never handle detached
  SQLAlchemy instances.
  """

  id: uuid.UUID
  chat_id: str
  message_id: int
  status: TradeStatusEnum

  model_config = {"from_attributes": True}


class TradeResponse(BaseModel):
  """API response model for a trade row."""

  id: uuid.UUID
  account_id: str
  market: Optional[MarketTypeEnum] = None
  gateway: Optional[str] = None
  account_leverage: Optional[int]
  account_balance_init: Optional[float]
  account_balance: Optional[float]
  ref_id: Optional[str]
  comment: Optional[str]
  strategy_code: str
  gateway_return_code: Optional[int]
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
  status: TradeStatusEnum
  # The event that last moved the trade (TP1/TP2/SL/R_SL/FLAT/...). Several map
  # onto the same status, so this is what says *how* a trade ended.
  last_action: Optional[str] = None
  reject_reason: Optional[str]
  createdAt: datetime
  updatedAt: datetime

  model_config = {
    "from_attributes": True,
    "json_schema_extra": {
      "example": {
        "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "account_id": "MT5-12345678",
        "market": "FOREX",
        "gateway": "MT5",
        "account_leverage": 100,
        "account_balance_init": 10000.0,
        "account_balance": 10250.75,
        "ref_id": "987654321",
        "comment": None,
        "strategy_code": "LONG|SIG-001",
        "gateway_return_code": 0,
        "strategy": "BTC-M15",
        "symbol": "BTCUSDT",
        "action": "LONG",
        "price": 65000.0,
        "quantity": 0.01,
        "sl": 63000.0,
        "tp1": 67000.0,
        "tp2": 69000.0,
        "is_running": True,
        "risk_percent": 1.0,
        "status": "OPENED",
        "last_action": "OPENED",
        "reject_reason": None,
        "createdAt": "2026-06-01T08:00:00Z",
        "updatedAt": "2026-06-02T09:30:00Z",
      }
    },
  }


class PageMeta(BaseModel):
  """Pagination metadata echoed back in every paginated response."""

  total: int
  limit: int
  offset: int
  order: Literal["asc", "desc"]
  order_by: str


class TradeListResponse(BaseModel):
  """Paginated response for the trades list endpoint."""

  data: List[TradeResponse]
  page: PageMeta
