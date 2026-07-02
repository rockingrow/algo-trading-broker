"""Tests for the IsAdmin router filter."""

from __future__ import annotations

from app.config import settings
from app.filters.admin import IsAdmin


class FakeUser:
  def __init__(self, user_id):
    self.id = user_id


class FakeEvent:
  def __init__(self, user):
    self.from_user = user


async def test_is_admin_true(monkeypatch):
  monkeypatch.setattr(settings, "TELEGRAM_ADMIN_IDS", "111, 222")
  assert await IsAdmin()(FakeEvent(FakeUser(222))) is True


async def test_is_admin_false(monkeypatch):
  monkeypatch.setattr(settings, "TELEGRAM_ADMIN_IDS", "111")
  assert await IsAdmin()(FakeEvent(FakeUser(999))) is False


async def test_is_admin_empty(monkeypatch):
  monkeypatch.setattr(settings, "TELEGRAM_ADMIN_IDS", "")
  assert await IsAdmin()(FakeEvent(FakeUser(111))) is False


async def test_is_admin_no_user():
  assert await IsAdmin()(FakeEvent(None)) is False
