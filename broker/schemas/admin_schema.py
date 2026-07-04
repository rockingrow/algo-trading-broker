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
