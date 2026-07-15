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

from broker.schemas.account_schema import MarketTypeEnum


class PositionEventType(str, Enum):
  """Whether this NATS TRADE event represents a newly opened position or an update to an existing one."""

  CREATED = "CREATED"
  UPDATED = "UPDATED"


class PositionEvent(BaseModel):
  """Wire payload published by a worker on the NATS TRADE subject whenever a position row is created or updated."""

  model_config = ConfigDict(use_enum_values=True)

  event: PositionEventType
  market_type: MarketTypeEnum
  strategy: str
  strategy_code: Optional[str] = None

  id: int
  signal_id: Optional[str] = None
  ref_source_id: str
  ref_id: str

  symbol: str
  action: str
  volume: float
  sl: Optional[float] = None
  tp1: Optional[float] = None
  tp2: Optional[float] = None
  risk_percent: float = 0.0
  opened_price: float
  closed_price: Optional[float] = None
  status: str
  # Why the worker refused to execute the order when ``status`` is ``REJECTED``
  # (e.g. its MAX ORDER limit was reached). Persisted onto the trade row.
  reject_reason: Optional[str] = None
  gateway_return_code: Optional[int] = None
  comment: Optional[str] = None
  message: Optional[str] = None
  created_at: Optional[str] = None
  updated_at: Optional[str] = None
  sync_status: Optional[str] = None
  sync_time: Optional[str] = None

  # Account snapshot — required to create a Trade record on the broker.
  account_leverage: Optional[int] = None
  account_balance: Optional[float] = None
  account_id: str
  account_name: Optional[str] = None
  # Exchange/gateway the account trades through (e.g. MT5, BINANCE). Upserted
  # onto the accounts row so the broker can address the worker as
  # <market_type>-<gateway>-<account_id> on the SYSTEM subject.
  gateway: Optional[str] = None
