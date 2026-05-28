from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from broker.schemas.core import SignalActionEnum


class PublishTopicEnum(str, Enum):
  """NATS subjects the broker publishes to or listens on for system-level and trading messages."""

  SIGNAL = "SIGNAL"
  ADMIN = "ADMIN"
  TRADE = "TRADE"


class TradingSignal(BaseModel):
  """Normalised signal produced from a TradingView webhook payload."""

  model_config = ConfigDict(use_enum_values=True)

  signal_id: str
  timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

  strategy: str
  action: SignalActionEnum
  symbol: str = Field(..., description="Instrument symbol, e.g. XAUUSD")
  price: float
  quantity: float
  sl: Optional[float] = None
  tp1: Optional[float] = None
  tp2: Optional[float] = None
  is_running: Optional[bool] = None
  risk_percent: Optional[float] = None
