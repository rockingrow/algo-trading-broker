import uuid
from typing import Optional
from pydantic import BaseModel


class SettingToggleResponse(BaseModel):
  setting: str
  value: str
  state: str


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
