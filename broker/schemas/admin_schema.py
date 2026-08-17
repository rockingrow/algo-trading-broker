import uuid
from typing import Optional
from pydantic import BaseModel, Field, model_validator

from broker.schemas.account_schema import MarketTypeEnum


class SettingToggleResponse(BaseModel):
  setting: str
  value: str
  state: str


class SettingValueResponse(BaseModel):
  """Response for admin endpoints that set a setting to an explicit value
  (as opposed to toggling a boolean flag)."""

  setting: str
  value: str


class RotateTokenResponse(BaseModel):
  """Result of rotating an account's bot link token."""

  account_id: str
  link_token: uuid.UUID


class AdminResponse(BaseModel):
  action: str
  scope: str


class CreateAccountRequest(BaseModel):
  """Request body for POST /admin/accounts — manually register an account
  ahead of any trade/handshake, e.g. so an admin can hand a link token to the
  end-user before they've placed a trade. ``gateway`` must be valid for
  ``market`` per ``GATEWAYS_BY_MARKET``. ``account_id`` may not contain
  ':' or whitespace — it's embedded verbatim in Telegram callback data."""

  market: MarketTypeEnum
  gateway: str = Field(..., min_length=1, max_length=50)
  account_id: str = Field(..., min_length=1, max_length=50, pattern=r"^[^:\s]+$")
  account_name: Optional[str] = None


class AdminLinkTelegramRequest(BaseModel):
  """Request body for POST /admin/accounts/{account_uuid}/link-telegram —
  admin-bind a Telegram user to an account directly, skipping the token flow."""

  telegram_user_id: int = Field(
    ..., description="Telegram user id to bind to the account."
  )


class FlatRequest(BaseModel):
  """Request body for the POST /flat admin endpoint.

  ``account_id`` alone no longer identifies a single account (the same bare
  id can exist under a different market/gateway, see
  ``uq_accounts_market_gateway_account_id``), so ``market`` and
  ``gateway`` are REQUIRED together with it — scoping a FLAT to one account
  without them is rejected. Omit all three to flat everything (no scoping
  needed). Forwarded onto the broadcast ``AdminSignal``; see that schema's
  docstring for the residual caveat about workers that don't check them.
  """

  strategy: Optional[str] = None
  symbol: Optional[str] = None
  account_id: Optional[str] = None
  market: Optional[MarketTypeEnum] = None
  gateway: Optional[str] = None

  @model_validator(mode="after")
  def _require_market_gateway_with_account_id(self) -> "FlatRequest":
    if self.account_id is not None and (self.market is None or self.gateway is None):
      raise ValueError(
        "market and gateway are required when account_id is given "
        "(account_id alone no longer identifies a single account)"
      )
    return self


class CryptoAllowedSymbolRequest(BaseModel):
  """Request body for POST /settings/crypto-allowed-symbol."""

  symbols: list[str] = Field(
    ..., min_length=1, description="Crypto symbols to allow, e.g. ['BTC', 'ETH']."
  )


class PublicBroadcastChatIdsRequest(BaseModel):
  """Request body for POST /settings/public-broadcast-chat-ids.

  The chats the **public** signal broadcast is delivered to. An empty list is
  allowed and meaningful — it turns the public broadcast off — which is why
  this has no ``min_length`` unlike the other list settings.
  """

  chat_ids: list[str] = Field(
    default_factory=list,
    description=(
      "Telegram chat ids (or @channel usernames) to broadcast publicly to, "
      "e.g. ['-1001234567890', '@my_public_channel']. Empty disables it."
    ),
  )


class CryptoMaxLeverageRequest(BaseModel):
  """Request body for POST /settings/crypto-max-leverage."""

  default_leverage: int = Field(
    ..., gt=0, description="Default leverage pushed to crypto workers on connect."
  )


class StrategyMagicMapRequest(BaseModel):
  """Request body for POST /settings/strategy-magic-map.

  The strategy → magic-number map, stored as JSON text and pushed to each
  worker (filtered to the strategies it announced) on connect. Values must be
  integers; at least one entry is required so an accidental empty submission
  can't wipe the configured map.
  """

  magic_map: dict[str, int] = Field(
    ...,
    min_length=1,
    description="Strategy name → magic number, e.g. {'MT5_GOLD_M5_V1': 20260409}.",
  )


class NotificationTimezoneRequest(BaseModel):
  """Request body for POST /settings/notification-timezone."""

  utc_offset_hours: float = Field(
    ...,
    ge=-12,
    le=14,
    description="UTC offset in hours applied to the 'Time:' line of Telegram "
    "notifications, e.g. 7 for UTC+7.",
  )
