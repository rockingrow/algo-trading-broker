"""
broker/schemas/webhook_schema.py — Pydantic models for validated TradingView webhooks.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict

from broker.schemas.core import SignalActionEnum


class PositionSchema(BaseModel):
  action: SignalActionEnum
  price: Optional[float] = None
  quantity: Optional[float] = None
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

  strategy: str
  symbol: str
  timeframe: str
  timestamp: datetime
  position: PositionSchema
  indicators: Optional[IndicatorsSchema] = None
  inputs: Optional[InputsSchema] = None
  token: str
