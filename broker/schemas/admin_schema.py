from typing import Optional
from pydantic import BaseModel


class SettingToggleResponse(BaseModel):
  setting: str
  value: str
  state: str


class AdminResponse(BaseModel):
  action: str
  scope: str


class FlatRequest(BaseModel):
  """Request body for the POST /flat admin endpoint."""

  strategy: Optional[str] = None
  symbol: Optional[str] = None
  account_id: Optional[str] = None
