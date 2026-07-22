"""
broker/security/ensure_api_key.py — API key authentication guard.
"""

from __future__ import annotations

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

from broker.settings import settings

API_KEY_HEADER_NAME = "X-API-KEY"

_api_key_header = APIKeyHeader(name=API_KEY_HEADER_NAME, auto_error=False)


async def ensure_api_key(api_key: str = Security(_api_key_header)) -> None:
  """Validate the X-API-KEY header against settings.broker_api.API_KEY."""
  if not settings.broker_api.API_KEY:
    raise HTTPException(
      status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
      detail="Broker API key not configured",
    )
  if api_key != settings.broker_api.API_KEY:
    raise HTTPException(
      status_code=status.HTTP_401_UNAUTHORIZED,
      detail="Invalid or missing API key",
    )
