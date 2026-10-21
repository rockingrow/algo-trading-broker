"""
broker/schemas/telegram_schema.py
─────────────────────────────────
Request/response models for the ``/v1/telegram/*`` endpoints consumed by the
Telegram bot service. These form the first slice of the future "User APIs":
the bot is a trusted internal caller (authenticated with ``X-API-KEY``) and the
end-user identity is the ``telegram_user_id`` supplied from a verified update.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

from broker.schemas.account_schema import MarketTypeEnum
from broker.schemas.trade_schema import TradeResponse


class LinkRequest(BaseModel):
  """Body for ``POST /v1/telegram/link`` — claim an account with its token."""

  token: uuid.UUID = Field(..., description="An account_link_tokens.token value.")
  telegram_user_id: int = Field(..., description="Telegram user id to bind.")


class LinkedAccountResponse(BaseModel):
  """Account summary returned to the bot after linking / resolving a user.

  Intentionally omits the link token — the end-user never needs to see it
  again once linked — and the caller's own id, which the bot already knows and
  which would be meaningless anyway: every account in this response is by
  definition linked to the caller.
  """

  id: uuid.UUID
  account_id: str
  account_name: Optional[str] = None
  account_balance: Optional[float] = None
  market: MarketTypeEnum
  gateway: Optional[str] = None
  last_activity_at: Optional[datetime] = None
  # Whether this is the user's currently active account (the one
  # single-account commands act on). Not an ORM column — set explicitly by
  # the endpoint after resolving the caller's BotSession.
  is_active: bool = False

  model_config = {"from_attributes": True}


class SwitchAccountRequest(BaseModel):
  """Body for ``POST /v1/telegram/{telegram_user_id}/active-account``."""

  account_id: uuid.UUID = Field(..., description="Row id of the account to activate.")


class FlatCommandRequest(BaseModel):
  """Optional scoping for a FLAT command. The account is resolved server-side
  from the path ``telegram_user_id`` and always added to the scope."""

  symbol: Optional[str] = None
  strategy: Optional[str] = None


class PreventCommandRequest(BaseModel):
  """Toggle new-entry blocking for the caller's account."""

  enabled: bool = Field(
    True, description="True publishes BLOCK_SIGNAL, False publishes ALLOW_SIGNAL."
  )


class MaxOpenOrdersRequest(BaseModel):
  """Set the caller's own cap on concurrently open positions.

  ``ge=0`` rather than ``gt=0``: ``0`` is the documented way to turn the cap
  off on the worker, not an invalid value. ``null`` clears the account's own
  setting so it goes back to inheriting the broker-wide default — a distinct
  intent from "no cap", which is why the field is nullable and required.
  """

  max_open_orders: Optional[int] = Field(
    ...,
    ge=0,
    description=(
      "Maximum concurrently open positions for this account. 0 disables the "
      "cap; null clears the override and inherits the global default."
    ),
  )


class MaxOpenOrdersResponse(BaseModel):
  """The cap in force for the caller's account after a read or a write.

  ``max_open_orders`` is the *effective* value the account's worker runs on, so
  a caller can render it without knowing the two scopes exist. ``source`` says
  which of them it came from, and ``global_max_open_orders`` is the value the
  account would fall back to — together they let the bot show "5 (global
  default)" versus "3 (yours)" and offer to clear an override.
  """

  max_open_orders: int = Field(
    ..., description="Effective cap for this account's worker."
  )
  source: Literal["account", "global"] = Field(
    ...,
    description=(
      "'account' when the owner set this value, 'global' when it is inherited."
    ),
  )
  global_max_open_orders: int = Field(
    ..., description="The broker-wide default this account falls back to."
  )


class CommandResultResponse(BaseModel):
  """Result of publishing a control command on behalf of a user."""

  action: str
  scope: str
  status: str = "published"
  # The account's settings blob after the command, for commands that change one
  # (``/prevent`` and ``/allow`` write ``signal_blocked``). ``None`` for a
  # command that only publishes and stores nothing, like FLAT.
  settings: Optional[dict] = Field(
    default=None,
    description="Account settings after the command; null if it changed none.",
  )


class BroadcastSubscriptionResponse(BaseModel):
  """Whether the caller is opted in to live trade-card DMs for their *active*
  account, and which account that is.

  The opt-in is per account, so the answer is meaningless without naming the
  account it was read from or written to — and the caller may have switched
  accounts since it last resolved one, so the response is the authority."""

  subscribed: bool
  account_id: str
  market: MarketTypeEnum
  gateway: Optional[str] = None


class BroadcastBulkSubscriptionResponse(BaseModel):
  """Outcome of switching live trade-card DMs on or off for **every** account
  the caller is linked to (the bot's ``/subscribeall`` / ``/unsubscribeall``)."""

  subscribed: bool
  # How many linked accounts the switch covered — the caller's links at the
  # moment of the call; an account linked afterwards starts off.
  accounts: int


class OpenPositionsResponse(BaseModel):
  """Currently running (open) trades for the caller's linked account.

  Unpaginated — ``data`` always carries every open position, and ``count``
  is simply its length, spelled out so a caller that only needs the number
  (the ``/status`` line) doesn't have to parse the list.
  """

  count: int
  data: list[TradeResponse]
