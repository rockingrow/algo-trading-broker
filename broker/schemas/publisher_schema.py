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


class AdminActionEnum(str, Enum):
  """Admin actions that can be published to the ADMIN topic."""

  FLAT = "FLAT"


class ScalingSchema(BaseModel):
  """Scaling block carrying the target levels and size used when scaling an existing position."""

  tp: Optional[float] = None
  sl: Optional[float] = None
  quantity: Optional[float] = None


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
  tp1_percent: Optional[float] = None
  move_sl_to_be: Optional[bool] = None
  is_running: Optional[bool] = None
  risk_percent: Optional[float] = None
  is_scale_position: Optional[bool] = None
  scale_strategy: Optional[str] = None
  scaling: Optional[ScalingSchema] = None


class AdminSignal(BaseModel):
  """Admin signal published to the ADMIN topic."""

  model_config = ConfigDict(
    use_enum_values=True,
    from_attributes=True,
    json_schema_extra={
      "example": {
        "action": "FLAT",
        "timestamp": "2026-06-02T08:00:00+00:00",
        "strategy": "my_strategy",
        "symbol": "XAUUSD",
        "account_id": "123456",
      }
    },
  )

  action: AdminActionEnum
  timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
  strategy: Optional[str] = None
  symbol: Optional[str] = None
  account_id: Optional[str] = None
