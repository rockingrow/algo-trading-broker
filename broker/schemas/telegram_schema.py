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
from typing import Optional

from pydantic import BaseModel, Field

from broker.schemas.account_schema import MarketTypeEnum


class LinkRequest(BaseModel):
  """Body for ``POST /v1/telegram/link`` — claim an account with its token."""

  token: uuid.UUID = Field(..., description="The account's telegram_link_token.")
  telegram_user_id: int = Field(..., description="Telegram user id to bind.")


class LinkedAccountResponse(BaseModel):
  """Account summary returned to the bot after linking / resolving a user.

  Intentionally omits ``telegram_link_token`` — the end-user never needs to see
  the token again once linked.
  """

  id: uuid.UUID
  account_id: str
  account_name: Optional[str] = None
  account_balance: Optional[float] = None
  market: MarketTypeEnum
  gateway: Optional[str] = None
  last_activity_at: Optional[datetime] = None
  telegram_user_id: Optional[int] = None
  # Whether this is the user's currently active account (the one
  # single-account commands act on). Not an ORM column — set explicitly by
  # the endpoint after resolving the caller's TelegramSession.
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
    True, description="True publishes BLOCK_ENTRIES, False publishes ALLOW_ENTRIES."
  )


class CommandResultResponse(BaseModel):
  """Result of publishing a control command on behalf of a user."""

  action: str
  scope: str
  status: str = "published"
