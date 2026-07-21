import pytest
from fastapi import HTTPException

from broker.security import ensure_api_key as mod
from broker.security.ensure_api_key import API_KEY_HEADER_NAME, ensure_api_key


def test_header_name_constant():
  assert API_KEY_HEADER_NAME == "X-API-KEY"


async def test_valid_key_passes(monkeypatch):
  monkeypatch.setattr(mod.settings.broker_api, "API_KEY", "secret-key")
  # No exception means success.
  await ensure_api_key(api_key="secret-key")


async def test_invalid_key_raises_401(monkeypatch):
  monkeypatch.setattr(mod.settings.broker_api, "API_KEY", "secret-key")
  with pytest.raises(HTTPException) as exc:
    await ensure_api_key(api_key="wrong")
  assert exc.value.status_code == 401


async def test_missing_key_raises_401(monkeypatch):
  monkeypatch.setattr(mod.settings.broker_api, "API_KEY", "secret-key")
  with pytest.raises(HTTPException) as exc:
    await ensure_api_key(api_key=None)
  assert exc.value.status_code == 401


async def test_unconfigured_key_raises_500(monkeypatch):
  monkeypatch.setattr(mod.settings.broker_api, "API_KEY", "")
  with pytest.raises(HTTPException) as exc:
    await ensure_api_key(api_key="anything")
  assert exc.value.status_code == 500
