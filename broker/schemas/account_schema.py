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
  createdAt: datetime
  updatedAt: datetime

  model_config = {"from_attributes": True}
