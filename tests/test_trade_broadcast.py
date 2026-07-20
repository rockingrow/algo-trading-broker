"""Tests for the completed-trade broadcast feature and the admin link-account
endpoint.

Covers three layers with in-memory fakes (no DB / NATS):
- ``TradeBroadcastService`` completion gating + owner fan-out,
- the ``/v1/telegram/*/broadcast*`` opt-in endpoints,
- the ``POST /admin/accounts/{uuid}/link-telegram`` endpoint.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from broker.db.models import Account, Trade
from broker.helpers.message_formatter import format_completed_trade_message
from broker.providers import (
  get_account_repository,
  get_trade_broadcast_repository,
)
from broker.router import get_core_router
from broker.schemas.account_schema import AccountLinkSummary, MarketTypeEnum
from broker.schemas.core import SignalActionEnum
from broker.schemas.trade_event_schema import PositionEvent, PositionEventType
from broker.schemas.trade_schema import TradeStatusEnum
from broker.security.ensure_api_key import ensure_api_key
from broker.services.trade_broadcast_service import TradeBroadcastService

API_KEY = "test-api-key"


# ── Fakes ───────────────────────────────────────────────────────────


class FakeBroadcastRepo:
  def __init__(self, subscribed: set[int] | None = None, targets: list[str] | None = None):
    self._subscribed = {str(u) for u in (subscribed or set())}
    self._targets = targets or []
    self.target_calls: list[tuple] = []

  async def subscribe(self, telegram_user_id, platform=None):
    self._subscribed.add(str(telegram_user_id))
    return True

  async def unsubscribe(self, telegram_user_id, platform=None):
    self._subscribed.discard(str(telegram_user_id))
    return True

  async def is_subscribed(self, telegram_user_id, platform=None):
    return str(telegram_user_id) in self._subscribed

  async def list_broadcast_targets(self, account_id, market, gateway, platform=None):
    self.target_calls.append((account_id, market, gateway))
    return list(self._targets)


class FakeSettingRepo:
  def __init__(self, values=None):
    self._values = values or {}

  async def get(self, key):
    return self._values.get(key)


class FakeOwnerNotifier:
  def __init__(self):
    self.sent: list[tuple[str, str]] = []

  async def send_to(self, chat_id, message_text):
    self.sent.append((chat_id, message_text))
    return True


class FakeAdminLinkRepo:
  """Minimal AccountRepository for the admin link-telegram endpoint."""

  def __init__(self, account: Account | None):
    self._account = account
    self.linked: list[tuple[uuid.UUID, int]] = []

  async def admin_link_telegram(self, account_uuid, telegram_user_id, platform=None):
    if self._account is None or account_uuid != self._account.id:
      return None
    self.linked.append((account_uuid, telegram_user_id))
    return self._account

  async def get_link_summaries(self, account_ids, platform=None):
    return {
      self._account.id: AccountLinkSummary(
        link_token=uuid.uuid4(), linked_user_ids=["999"]
      )
    }


def _make_account() -> Account:
  return Account(
    id=uuid.uuid4(),
    account_id="acc-1",
    account_name="Main",
    account_balance=1000.0,
    market=MarketTypeEnum.FOREX,
    gateway="MT5",
    last_activity_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    createdAt=datetime(2026, 1, 1, tzinfo=timezone.utc),
    updatedAt=datetime(2026, 1, 1, tzinfo=timezone.utc),
  )


def _make_trade(status=TradeStatusEnum.CLOSED) -> Trade:
  return Trade(
    id=uuid.uuid4(),
    account_id="acc-1",
    market=MarketTypeEnum.FOREX,
    gateway="MT5",
    account_balance_init=1000.0,
    account_balance=1120.0,
    ref_id="ref-1",
    strategy="BTC-M15",
    strategy_code="LONG|SIG-1",
    symbol="BTCUSDT",
    action=SignalActionEnum.LONG,
    price=65000.0,
    quantity=0.01,
    is_running=False,
    risk_percent=1.0,
    status=status,
    createdAt=datetime(2026, 1, 1, tzinfo=timezone.utc),
    updatedAt=datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc),
  )


def _event(status: str) -> PositionEvent:
  return PositionEvent(
    event=PositionEventType.UPDATED,
    market=MarketTypeEnum.FOREX,
    strategy="BTC-M15",
    id=1,
    ref_source_id="ref-1",
    ref_id="ref-1",
    symbol="BTCUSDT",
    action="LONG",
    volume=0.01,
    opened_price=65000.0,
    status=status,
    account_id="acc-1",
    gateway="MT5",
  )


# ── TradeBroadcastService ────────────────────────────────────────────


async def test_broadcast_service_dms_subscribed_owners_on_close():
  repo = FakeBroadcastRepo(targets=["111", "222"])
  notifier = FakeOwnerNotifier()
  svc = TradeBroadcastService(
    broadcast_repository=repo,
    setting_repository=FakeSettingRepo(),
    notifier=notifier,
  )

  await svc.maybe_broadcast(_event("TP2"), _make_trade())

  assert [c for c, _ in notifier.sent] == ["111", "222"]
  assert repo.target_calls == [("acc-1", MarketTypeEnum.FOREX, "MT5")]


async def test_broadcast_service_skips_non_completion():
  repo = FakeBroadcastRepo(targets=["111"])
  notifier = FakeOwnerNotifier()
  svc = TradeBroadcastService(
    broadcast_repository=repo,
    setting_repository=FakeSettingRepo(),
    notifier=notifier,
  )

  # OPENED / TP1 (partial) are not completions.
  await svc.maybe_broadcast(_event("OPENED"), _make_trade(TradeStatusEnum.OPENED))
  await svc.maybe_broadcast(_event("TP1"), _make_trade(TradeStatusEnum.PARTIALLY_CLOSED))

  assert notifier.sent == []


async def test_broadcast_service_no_targets_no_send():
  notifier = FakeOwnerNotifier()
  svc = TradeBroadcastService(
    broadcast_repository=FakeBroadcastRepo(targets=[]),
    setting_repository=FakeSettingRepo(),
    notifier=notifier,
  )
  await svc.maybe_broadcast(_event("SL"), _make_trade())
  assert notifier.sent == []


async def test_broadcast_service_handles_none_trade():
  notifier = FakeOwnerNotifier()
  svc = TradeBroadcastService(
    broadcast_repository=FakeBroadcastRepo(targets=["111"]),
    setting_repository=FakeSettingRepo(),
    notifier=notifier,
  )
  await svc.maybe_broadcast(_event("TP2"), None)
  assert notifier.sent == []


def test_format_completed_trade_message_has_pnl():
  msg = format_completed_trade_message(_make_trade())
  assert "Trade completed" in msg
  assert "BTCUSDT" in msg
  assert "+120.00" in msg  # 1120 - 1000
  assert "CLOSED" in msg


# ── Broadcast opt-in endpoints ───────────────────────────────────────


@pytest.fixture
def broadcast_ctx():
  app = FastAPI()
  app.include_router(get_core_router())
  repo = FakeBroadcastRepo()
  app.dependency_overrides[get_trade_broadcast_repository] = lambda: repo
  app.dependency_overrides[ensure_api_key] = lambda: None
  return {"client": TestClient(app), "repo": repo}


def test_subscribe_then_status_then_unsubscribe(broadcast_ctx):
  client = broadcast_ctx["client"]
  h = {"X-API-KEY": API_KEY}

  r = client.get("/v1/telegram/555/broadcast", headers=h)
  assert r.status_code == 200 and r.json() == {"subscribed": False}

  r = client.post("/v1/telegram/555/broadcast/subscribe", headers=h)
  assert r.status_code == 200 and r.json() == {"subscribed": True}

  r = client.get("/v1/telegram/555/broadcast", headers=h)
  assert r.json() == {"subscribed": True}

  r = client.post("/v1/telegram/555/broadcast/unsubscribe", headers=h)
  assert r.status_code == 200 and r.json() == {"subscribed": False}


# ── Admin link-telegram endpoint ─────────────────────────────────────


def test_admin_link_telegram_success():
  account = _make_account()
  repo = FakeAdminLinkRepo(account)
  app = FastAPI()
  app.include_router(get_core_router())
  app.dependency_overrides[get_account_repository] = lambda: repo
  app.dependency_overrides[ensure_api_key] = lambda: None
  client = TestClient(app)

  r = client.post(
    f"/admin/accounts/{account.id}/link-telegram",
    headers={"X-API-KEY": API_KEY},
    json={"telegram_user_id": 999},
  )
  assert r.status_code == 200
  body = r.json()
  assert body["account_id"] == "acc-1"
  assert body["linked_user_ids"] == ["999"]
  assert repo.linked == [(account.id, 999)]


def test_admin_link_telegram_unknown_account():
  account = _make_account()
  repo = FakeAdminLinkRepo(account)
  app = FastAPI()
  app.include_router(get_core_router())
  app.dependency_overrides[get_account_repository] = lambda: repo
  app.dependency_overrides[ensure_api_key] = lambda: None
  client = TestClient(app)

  r = client.post(
    f"/admin/accounts/{uuid.uuid4()}/link-telegram",
    headers={"X-API-KEY": API_KEY},
    json={"telegram_user_id": 999},
  )
  assert r.status_code == 404
