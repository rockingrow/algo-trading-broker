"""
broker/schemas/account_schema.py
──────────────────────────────
Status enum shared by the Account ORM model and the NATS ACCOUNT handler.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel


class MarketTypeEnum(str, Enum):
  """Market segment that a trading account operates in."""

  FOREX = "FOREX"
  CRYPTO = "CRYPTO"


# Gateways an admin may register an account under, per market. The broker is
# the source of truth for this mapping — it's mirrored (for keyboard display
# only) in the bot's app/constants.py, which never validates against it.
GATEWAYS_BY_MARKET: dict[MarketTypeEnum, list[str]] = {
  MarketTypeEnum.FOREX: ["MT5"],
  MarketTypeEnum.CRYPTO: ["BINANCE"],
}


def compose_worker_id(market: str, gateway: str, account_id: str) -> str:
  """Build the ``<market>-<gateway>-<account_id>`` worker addressing id used on
  the SYSTEM subject from an account's parts (e.g. ``CRYPTO-BINANCE-7654321``).

  ``market`` may be a :class:`MarketTypeEnum` or its string value; both
  render to the bare market name.
  """
  market = market.value if isinstance(market, MarketTypeEnum) else str(market)
  return f"{market}-{gateway}-{account_id}"


def decompose_worker_id(worker_id: str, market: str, gateway: str) -> str:
  """Inverse of :func:`compose_worker_id`: recover the bare ``account_id`` (the
  one stored in the ``accounts`` table) from a worker id.

  Workers announce themselves on the SYSTEM subject with their full worker id
  (``CRYPTO-BINANCE-7654321``), while the ``accounts`` row is keyed by the bare
  ``account_id`` (``7654321``). A worker that sends the bare id instead carries
  no prefix to strip, so it passes through unchanged.
  """
  market = market.value if isinstance(market, MarketTypeEnum) else str(market)
  return worker_id.removeprefix(f"{market}-{gateway}-")


@dataclass
class AccountLinkSummary:
  """The bot-facing facts about an account that no longer live on its row: the
  token an admin can hand out, and which platform users are linked to it.

  Lives here rather than in the repository so the ``AccountRepository``
  protocol can name it without depending on the SQLAlchemy implementation.
  """

  link_token: Optional[uuid.UUID] = None
  linked_user_ids: list[str] = field(default_factory=list)


class AccountResponse(BaseModel):
  """API response model for a registered trading account, serialised from the Account ORM row."""

  id: uuid.UUID
  account_id: str
  account_name: Optional[str]
  account_balance: Optional[float]
  market: MarketTypeEnum
  gateway: Optional[str]
  last_activity_at: Optional[datetime]
  # Bot binding, joined in from account_link_tokens / account_bot_links rather
  # than read off the account row. ``link_token`` is exposed here only because
  # this endpoint is X-API-KEY protected (admin) — admins hand it to end-users.
  # ``linked_user_ids`` holds platform user ids as strings (an account may be
  # managed by several people); it is empty for an unclaimed account.
  link_token: Optional[uuid.UUID] = None
  linked_user_ids: list[str] = []
  createdAt: datetime
  updatedAt: datetime

  model_config = {
    "from_attributes": True,
    "json_schema_extra": {
      "example": {
        "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "account_id": "12345678",
        "account_name": "Main Forex",
        "account_balance": 10250.75,
        "market": "FOREX",
        "gateway": "MT5",
        "last_activity_at": "2026-06-02T09:30:00Z",
        "link_token": "b5dc0374-9639-4861-acf4-2d239aa5c1b4",
        "linked_user_ids": [],
        "createdAt": "2026-01-15T08:00:00Z",
        "updatedAt": "2026-06-02T09:30:00Z",
      }
    },
  }
