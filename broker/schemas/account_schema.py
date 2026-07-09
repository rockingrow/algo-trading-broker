"""
broker/schemas/account_schema.py
──────────────────────────────
Status enum shared by the Account ORM model and the NATS ACCOUNT handler.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel


class MarketTypeEnum(str, Enum):
  """Market segment that a trading account operates in."""

  FOREX = "FOREX"
  CRYPTO = "CRYPTO"


def compose_worker_id(market_type: str, gateway: str, account_id: str) -> str:
  """Build the ``<market>-<gateway>-<account_id>`` worker addressing id used on
  the SYSTEM subject from an account's parts (e.g. ``CRYPTO-BINANCE-7654321``).

  ``market_type`` may be a :class:`MarketTypeEnum` or its string value; both
  render to the bare market name.
  """
  market = (
    market_type.value if isinstance(market_type, MarketTypeEnum) else str(market_type)
  )
  return f"{market}-{gateway}-{account_id}"


class AccountResponse(BaseModel):
  """API response model for a registered trading account, serialised from the Account ORM row."""

  id: uuid.UUID
  account_id: str
  account_name: Optional[str]
  account_balance: Optional[float]
  market_type: MarketTypeEnum
  gateway: Optional[str]
  last_activity_at: Optional[datetime]
  createdAt: datetime
  updatedAt: datetime

  model_config = {
    "from_attributes": True,
    "json_schema_extra": {
      "example": {
        "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "account_id": "12345678",
        "account_name": "Main Forex",
        "account_balance": 10250.75,
        "market_type": "FOREX",
        "gateway": "MT5",
        "last_activity_at": "2026-06-02T09:30:00Z",
        "createdAt": "2026-01-15T08:00:00Z",
        "updatedAt": "2026-06-02T09:30:00Z",
      }
    },
  }
