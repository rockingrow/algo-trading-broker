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


class AccountResponse(BaseModel):
  """API response model for a registered trading account, serialised from the Account ORM row."""

  id: uuid.UUID
  account_id: str
  account_name: Optional[str]
  account_balance: Optional[float]
  market_type: MarketTypeEnum
  last_activity_at: Optional[datetime]
  # Telegram binding. ``telegram_link_token`` is exposed here only because this
  # endpoint is X-API-KEY protected (admin) — admins hand it to end-users.
  telegram_user_id: Optional[int] = None
  telegram_link_token: Optional[uuid.UUID] = None
  createdAt: datetime
  updatedAt: datetime

  model_config = {
    "from_attributes": True,
    "json_schema_extra": {
      "example": {
        "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "account_id": "MT5-12345678",
        "account_name": "Main Forex",
        "account_balance": 10250.75,
        "market_type": "FOREX",
        "last_activity_at": "2026-06-02T09:30:00Z",
        "telegram_user_id": None,
        "telegram_link_token": "b5dc0374-9639-4861-acf4-2d239aa5c1b4",
        "createdAt": "2026-01-15T08:00:00Z",
        "updatedAt": "2026-06-02T09:30:00Z",
      }
    },
  }
