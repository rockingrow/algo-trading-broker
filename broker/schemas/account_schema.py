"""
broker/schemas/account_schema.py
──────────────────────────────
Status enum shared by the Account ORM model and the NATS ACCOUNT handler.
"""

from __future__ import annotations

from enum import Enum


class MarketTypeEnum(str, Enum):
  FOREX = "FOREX"
  CRYPTO = "CRYPTO"
