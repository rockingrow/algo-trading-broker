"""
broker/schemas/trade_schema.py
──────────────────────────────
Status enum shared by the Trade ORM model and the NATS TRADE handler.
"""

from __future__ import annotations

from enum import Enum


class TradeStatusEnum(str, Enum):
  OPENED = "OPENED"
  REJECTED = "REJECTED"
  PARTIALLY_CLOSED = "PARTIALLY_CLOSED"
  CLOSED = "CLOSED"
  FLAT = "FLAT"
