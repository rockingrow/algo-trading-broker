"""
broker/models.py — Pydantic models for signals and API responses.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class SignalAction(str, Enum):
    open = "open"
    close = "close"
    close_all = "close_all"
    modify = "modify"


class OrderDirection(str, Enum):
    buy = "buy"
    sell = "sell"


class TradingSignal(BaseModel):
    """Normalised signal produced from a TradingView webhook payload."""

    signal_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    action: SignalAction
    symbol: str = Field(..., description="Instrument symbol, e.g. XAUUSD")
    direction: Optional[OrderDirection] = None   # required for 'open'

    volume: Optional[float] = None               # lot size
    sl: Optional[float] = None                   # stop loss price
    tp: Optional[float] = None                   # take profit price

    # For 'modify' or targeted 'close' — ticket id from MT5
    ticket: Optional[int] = None

    comment: Optional[str] = "TV_Signal"
    magic: Optional[int] = None                  # EA magic number

    class Config:
        use_enum_values = True


class WebhookPayload(BaseModel):
    """Raw TradingView alert JSON (flexible — unknown keys ignored)."""

    action: str
    symbol: str
    direction: Optional[str] = None
    volume: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    ticket: Optional[int] = None
    comment: Optional[str] = None
    magic: Optional[int] = None

    class Config:
        extra = "allow"


class BrokerStatus(BaseModel):
    """Response from /status endpoint."""

    uptime_seconds: float
    signals_received: int
    signals_published: int
    last_signal: Optional[TradingSignal] = None
