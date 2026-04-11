"""
broker/schemas/webhook.py — Pydantic models for validated TradingView webhooks.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class SignalActionEnum(str, Enum):
  LONG = "LONG"
  SHORT = "SHORT"
  TP1 = "TP1"
  TP2 = "TP2"
  R_SL = "R_SL"
  SL = "SL"


class PositionSchema(BaseModel):
  action: SignalActionEnum
  price: float
  quantity: float
  sl: Optional[float] = None
  tp1: Optional[float] = None
  tp2: Optional[float] = None
  is_running: Optional[bool] = None


class IndicatorsSchema(BaseModel):
  model_config = ConfigDict(extra="allow")

  wt1: Optional[float] = None
  wt2: Optional[float] = None
  close: Optional[float] = None
  upper: Optional[float] = None
  lower: Optional[float] = None
  basis: Optional[float] = None
  ema200_visual: Optional[float] = None
  ema200_filter: Optional[float] = None
  atr_val: Optional[float] = None
  in_session: Optional[bool] = None
  vol_spike: Optional[bool] = None
  in_cooldown: Optional[bool] = None
  sl_too_wide: Optional[bool] = None


class InputsSchema(BaseModel):
  model_config = ConfigDict(extra="allow")

  watching_candles: Optional[int] = None
  over_wt_value: Optional[int] = None
  safe_wt_value: Optional[int] = None
  use_session: Optional[bool] = None
  skip_windows: Optional[str] = None
  bb_len: Optional[int] = None
  bb_mult: Optional[float] = None
  show_imb: Optional[bool] = None
  atr_len: Optional[int] = None
  atr_sl_mult: Optional[float] = None
  min_rr_ratio: Optional[float] = None
  risk_percent: Optional[float] = None
  tp1_qty_pc: Optional[float] = None
  use_max_sl_dist: Optional[bool] = None
  max_sl_distance: Optional[int] = None
  vol_spike_mult: Optional[float] = None
  vol_spike_lookback: Optional[int] = None
  use_vol_filter: Optional[bool] = None
  ema_visual_tf: Optional[str] = None
  ema_filter_tf: Optional[str] = None
  use_cooldown: Optional[bool] = None
  cooldown_bars: Optional[int] = None


class WebhookPayload(BaseModel):
  """Raw TradingView alert JSON payload."""

  token: str
  symbol: str
  timeframe: str
  timestamp: datetime
  position: PositionSchema
  indicators: IndicatorsSchema
  inputs: InputsSchema


class TradingSignal(BaseModel):
  """Normalised signal produced from a TradingView webhook payload."""

  model_config = ConfigDict(use_enum_values=True)

  signal_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
  timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

  action: SignalActionEnum
  symbol: str = Field(..., description="Instrument symbol, e.g. XAUUSD")

  price: float
  volume: float
  sl: Optional[float] = None
  tp: Optional[float] = None

  # Fields used by DB logging
  ticket: Optional[int] = None

  # Meta info
  comment: Optional[str] = "TV_Signal"
  magic: str = Field(default_factory=lambda: str(uuid.uuid4()))


class BrokerStatus(BaseModel):
  """Response from /status endpoint."""

  uptime_seconds: float
  signals_received: int
  signals_published: int
  last_signal: Optional[TradingSignal] = None
