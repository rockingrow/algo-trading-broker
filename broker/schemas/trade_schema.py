"""
broker/schemas/trade_schema.py
──────────────────────────────
Status enum shared by the Trade ORM model and the NATS TRADE handler.
"""

from __future__ import annotations

from enum import Enum


class TradeStatusEnum(str, Enum):
  """Lifecycle states of a trade row, progressing from open through partial fills to close or rejection."""

  OPENED = "OPENED"
  REJECTED = "REJECTED"
  PARTIALLY_CLOSED = "PARTIALLY_CLOSED"
  CLOSED = "CLOSED"
  FLAT = "FLAT"
