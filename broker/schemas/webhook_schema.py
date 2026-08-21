"""
broker/schemas/webhook_schema.py — Pydantic models for validated TradingView webhooks.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from broker.helpers.uxid_helper import UXID_LENGTH, is_valid_uxid, new_uxid
from broker.schemas.core import SignalActionEnum


class ScalingSchema(BaseModel):
  """Scaling block carrying the target levels and size used when scaling an existing position."""

  tp: Optional[float] = None
  sl: Optional[float] = None
  quantity: Optional[float] = None


class PositionSchema(BaseModel):
  """Nested position block within a TradingView webhook, carrying action, price, size, and risk levels."""

  action: SignalActionEnum
  price: Optional[float] = None
  quantity: Optional[float] = None
  sl: Optional[float] = None
  tp1: Optional[float] = None
  tp2: Optional[float] = None
  risk_percent: Optional[float] = None
  tp1_percent: Optional[float] = None
  move_sl_to_be: Optional[bool] = None
  is_running: Optional[bool] = None
  is_scale_position: Optional[bool] = None
  scale_strategy: Optional[str] = None
  scaling: Optional[ScalingSchema] = None


class IndicatorsSchema(BaseModel):
  """Snapshot of strategy indicator values at signal time; unknown extra fields are accepted and preserved."""

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
  """Strategy input parameters sent alongside each signal for audit; extra fields are accepted and preserved."""

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

  model_config = ConfigDict(
    json_schema_extra={
      "example": {
        "strategy": "wt_bb_atr",
        "symbol": "BTCUSDT",
        "timeframe": "15",
        "timestamp": "2026-06-02T10:15:00Z",
        "signal_uxid": "9f2c4b7e18a3d605",
        "position": {
          "action": "LONG",
          "price": 68250.5,
          "quantity": 0.05,
          "sl": 67800.0,
          "tp1": 68900.0,
          "tp2": 69500.0,
          "is_running": True,
        },
        "token": "shared-webhook-token",
      }
    }
  )

  strategy: str
  symbol: str
  timeframe: str
  timestamp: datetime
  # Short id (exactly 16 lowercase-hex chars, i.e. what :func:`new_uxid`
  # produces) shared by every alert of one trade cycle: the entry and each of
  # its TP/SL/FLAT follow-ups. Together with ``strategy`` it keys the single
  # Telegram broadcast message the cycle is rendered into, so a strategy that
  # wants one grouped message must send the same value on every alert of that
  # trade. Generated per payload when TradingView omits it, which degrades to
  # the old behaviour: one message per signal.
  signal_uxid: str = Field(default_factory=new_uxid)
  position: PositionSchema
  indicators: Optional[IndicatorsSchema] = None
  inputs: Optional[InputsSchema] = None
  token: str

  @field_validator("signal_uxid", mode="before")
  @classmethod
  def _fill_or_validate_uxid(cls, value):
    """Auto-generate when the payload has none; strictly validate what it has.

    A blank (``null`` / ``""`` / whitespace) is treated as an omitted field:
    TradingView alert templates commonly interpolate an empty placeholder
    rather than dropping the key, and without this every such payload would
    key its cycle to the same blank id and collapse unrelated trades into one
    message.

    Anything else is rejected unless it matches the exact uxid shape (16
    uppercase alphanumeric characters). Uppercase is accepted and normalised, so a
    strategy that writes UUIDs in either case works either way. Rejecting at
    the boundary is deliberate: a malformed id — a shortened one, a UUID with
    dashes, a random string — could collide against a real cycle id and quietly
    merge two unrelated trades. Failing fast with a 422 forces the strategy to
    fix its alert instead.
    """
    if value is None:
      return new_uxid()
    text = str(value).strip()
    if not text:
      return new_uxid()
    normalised = text.upper()
    if not is_valid_uxid(normalised):
      raise ValueError(
        f"signal_uxid must be exactly {UXID_LENGTH} uppercase alphanumeric characters, "
        f"got {value!r}"
      )
    return normalised
