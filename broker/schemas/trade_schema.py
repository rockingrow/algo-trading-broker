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

from broker.schemas.core import SignalActionEnum


class TradeStatusEnum(str, Enum):
  """Lifecycle states of a trade row, progressing from open through partial fills to close or rejection."""

  OPENED = "OPENED"
  REJECTED = "REJECTED"
  PARTIALLY_CLOSED = "PARTIALLY_CLOSED"
  CLOSED = "CLOSED"
  FLAT = "FLAT"


class TradeResponse(BaseModel):
  """API response model for a trade row."""

  id: uuid.UUID
  account_id: str
  account_leverage: int
  account_balance_init: Optional[float]
  account_balance: Optional[float]
  ref_id: Optional[str]
  comment: Optional[str]
  strategy_code: str
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
  reject_reason: Optional[str]
  createdAt: datetime
  updatedAt: datetime

  model_config = {
    "from_attributes": True,
    "json_schema_extra": {
      "example": {
        "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "account_id": "MT5-12345678",
        "account_leverage": 100,
        "account_balance_init": 10000.0,
        "account_balance": 10250.75,
        "ref_id": "987654321",
        "comment": None,
        "strategy_code": "LONG|SIG-001",
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
