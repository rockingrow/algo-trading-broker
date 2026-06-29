import uuid
from typing import Optional
from pydantic import BaseModel, Field


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


class FlatRequest(BaseModel):
  """Request body for the POST /flat admin endpoint."""

  strategy: Optional[str] = None
  symbol: Optional[str] = None
  account_id: Optional[str] = None


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
