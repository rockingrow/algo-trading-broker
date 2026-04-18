from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from broker.schemas.core import SignalActionEnum


class PublishTopicEnum(str, Enum):
  SIGNAL = "SIGNAL"
  ADMIN = "ADMIN"


class TradingSignal(BaseModel):
  """Normalised signal produced from a TradingView webhook payload."""

  model_config = ConfigDict(use_enum_values=True)

  signal_id: str
  timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

  action: SignalActionEnum
  symbol: str = Field(..., description="Instrument symbol, e.g. XAUUSD")
  price: float
  quantity: float
  sl: Optional[float] = None
  tp1: Optional[float] = None
  tp2: Optional[float] = None
  is_running: Optional[bool] = None
  risk_percent: Optional[float] = None
