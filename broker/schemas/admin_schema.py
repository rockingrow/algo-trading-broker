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
  """Result of rotating an account's Telegram link token."""

  account_id: str
  telegram_link_token: uuid.UUID


class AdminResponse(BaseModel):
  action: str
  scope: str


class CreateAccountRequest(BaseModel):
  """Request body for POST /admin/accounts — manually register an account
  ahead of any trade/handshake, e.g. so an admin can hand a link token to the
  end-user before they've placed a trade. ``gateway`` must be valid for
  ``market_type`` per ``GATEWAYS_BY_MARKET``. ``account_id`` may not contain
  ':' or whitespace — it's embedded verbatim in Telegram callback data."""

  market_type: MarketTypeEnum
  gateway: str = Field(..., min_length=1, max_length=50)
  account_id: str = Field(..., min_length=1, max_length=50, pattern=r"^[^:\s]+$")
  account_name: Optional[str] = None


class FlatRequest(BaseModel):
  """Request body for the POST /flat admin endpoint.

  ``account_id`` alone no longer identifies a single account (the same bare
  id can exist under a different market/gateway, see
  ``uq_accounts_market_gateway_account_id``), so ``market_type`` and
  ``gateway`` are REQUIRED together with it — scoping a FLAT to one account
  without them is rejected. Omit all three to flat everything (no scoping
  needed). Forwarded onto the broadcast ``AdminSignal``; see that schema's
  docstring for the residual caveat about workers that don't check them.
  """

  strategy: Optional[str] = None
  symbol: Optional[str] = None
  account_id: Optional[str] = None
  market_type: Optional[MarketTypeEnum] = None
  gateway: Optional[str] = None

  @model_validator(mode="after")
  def _require_market_gateway_with_account_id(self) -> "FlatRequest":
    if self.account_id is not None and (self.market_type is None or self.gateway is None):
      raise ValueError(
        "market_type and gateway are required when account_id is given "
        "(account_id alone no longer identifies a single account)"
      )
    return self


class CryptoAllowedSymbolRequest(BaseModel):
  """Request body for POST /settings/crypto-allowed-symbol."""

  symbols: list[str] = Field(
    ..., min_length=1, description="Crypto symbols to allow, e.g. ['BTC', 'ETH']."
  )


class CryptoMaxLeverageRequest(BaseModel):
  """Request body for POST /settings/crypto-max-leverage."""

  default_leverage: int = Field(
    ..., gt=0, description="Default leverage pushed to crypto workers on connect."
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
