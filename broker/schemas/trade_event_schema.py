"""
broker/schemas/trade_event_schema.py
────────────────────────────────────
Schema for position events received on the NATS TRADE subject, published by
the worker whenever a row in its SQLite `positions` table is inserted or
updated. Mirrors `worker/schemas/trade_event_schema.py` on the wire.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict


class PositionEventType(str, Enum):
  CREATED = "CREATED"
  UPDATED = "UPDATED"


class PositionEvent(BaseModel):
  model_config = ConfigDict(use_enum_values=True)

  event: PositionEventType
  market_type: str

  id: int
  source_ticket: int
  ticket: int
  strategy: str
  symbol: str
  action: str
  volume: float
  opened_price: float
  closed_price: Optional[float] = None
  status: str
  mt5_retcode: Optional[int] = None
  comment: Optional[str] = None
  message: Optional[str] = None
  created_at: Optional[str] = None
  updated_at: Optional[str] = None
  sync_status: Optional[str] = None
  sync_time: Optional[str] = None

  # Signal-derived fields (worker parses `message` JSON before publishing).
  signal_id: Optional[str] = None
  magic: Optional[str] = None
  sl: Optional[float] = None
  tp1: Optional[float] = None
  tp2: Optional[float] = None
  risk_percent: float = 0.0

  # MT5 account snapshot from the worker — needed to create a Trade row.
  account_id: str
  account_name: str
  account_leverage: Optional[int] = None
  account_balance: Optional[float] = None
