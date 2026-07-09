"""End-to-end-ish tests for the HTTP layer using FastAPI's TestClient.

The app is assembled from the real routers but every infrastructure
dependency (repositories, publisher, notifier, signal service) is replaced
with an in-memory fake via ``dependency_overrides`` so no DB/NATS is needed.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from broker.constants import (
  CRYPTO_ALLOWED_SYMBOL_KEY,
  CRYPTO_MAX_LEVERAGE_KEY,
  NOTIFICATION_TIMEZONE_KEY,
  SIGNAL_BLOCKED,
)
from broker.db.models import Account, Trade
from broker.providers import (
  get_account_repository,
  get_admin_notifier,
  get_publisher,
  get_setting_repository,
  get_signal_service,
  get_trade_repository,
)
from broker.router import get_core_router
from broker.schemas.trade_schema import TradeStatusEnum
from broker.schemas.account_schema import MarketTypeEnum
from broker.schemas.core import SignalActionEnum
from broker.schemas.publisher_schema import (
  SYSTEM_BROADCAST_ACCOUNT_ID,
  SystemActionEnum,
)
from broker.security.ensure_api_key import ensure_api_key

API_KEY = "test-api-key"


# ── Fakes ───────────────────────────────────────────────────────────


class FakeSignalService:
  def __init__(self):
    self.calls = []

  async def process(self, payload):
    self.calls.append(payload)
    return {
      "status": "accepted",
      "signal_id": "sig-1",
      "timestamp": payload.timestamp.isoformat(),
    }


class FakeSettingRepo:
  def __init__(self):
    self.values = {}
    self.fail_set = False

  async def get(self, key):
    return self.values.get(key)

  async def get_many(self, keys):
    return {k: v for k, v in self.values.items() if k in keys and v is not None}

  async def set(self, key, value):
    if self.fail_set:
      return False
    self.values[key] = value
    return True


class FakeNotifier:
  def __init__(self):
    self.messages = []

  async def send_message(self, message_text):
    self.messages.append(message_text)


class FakePublisher:
  def __init__(self):
    self.admin_signals = []
    self.system_signals = []

  async def publish_admin_signal(self, **kwargs):
    self.admin_signals.append(kwargs)

  async def publish_system_signal(self, **kwargs):
    self.system_signals.append(kwargs)


class FakeAccountRepo:
  def __init__(self, accounts):
    self._accounts = accounts

  async def get_all(self):
    return self._accounts


class FakeTradeRepo:
  def __init__(self, trades, total):
    self._trades = trades
    self._total = total
    self.list_kwargs = None

  async def list_by_account(self, account_id, *, limit, offset, order, order_by):
    self.list_kwargs = dict(
      account_id=account_id, limit=limit, offset=offset, order=order, order_by=order_by
    )
    return self._trades

  async def count_by_account(self, account_id):
    return self._total


def _make_account() -> Account:
  return Account(
    id=uuid.uuid4(),
    account_id="acc-1",
    account_name="Main",
    account_balance=1000.0,
    market_type=MarketTypeEnum.FOREX,
    last_activity_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    createdAt=datetime(2026, 1, 1, tzinfo=timezone.utc),
    updatedAt=datetime(2026, 1, 1, tzinfo=timezone.utc),
  )


def _make_trade() -> Trade:
  return Trade(
    id=uuid.uuid4(),
    account_id="acc-1",
    account_leverage=100,
    account_balance_init=1000.0,
    account_balance=1010.0,
    ref_id="r-1",
    comment=None,
    gateway_return_code=0,
    strategy="strat",
    strategy_code="LONG|sig",
    symbol="XAUUSD",
    action=SignalActionEnum.LONG,
    price=100.0,
    quantity=1.0,
    sl=95.0,
    tp1=110.0,
    tp2=120.0,
    is_running=True,
    risk_percent=1.0,
    status=TradeStatusEnum.OPENED,
    reject_reason=None,
    createdAt=datetime(2026, 1, 1, tzinfo=timezone.utc),
    updatedAt=datetime(2026, 1, 1, tzinfo=timezone.utc),
  )


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def ctx():
  """Build an app with all infra dependencies overridden by fakes."""
  app = FastAPI()
  app.include_router(get_core_router())

  signal_service = FakeSignalService()
  setting_repo = FakeSettingRepo()
  notifier = FakeNotifier()
  publisher = FakePublisher()
  account_repo = FakeAccountRepo([_make_account()])
  trade_repo = FakeTradeRepo([_make_trade()], total=1)

  app.dependency_overrides[get_signal_service] = lambda: signal_service
  app.dependency_overrides[get_setting_repository] = lambda: setting_repo
  app.dependency_overrides[get_admin_notifier] = lambda: notifier
  app.dependency_overrides[get_publisher] = lambda: publisher
  app.dependency_overrides[get_account_repository] = lambda: account_repo
  app.dependency_overrides[get_trade_repository] = lambda: trade_repo
  app.dependency_overrides[ensure_api_key] = lambda: None

  client = TestClient(app)
  return {
    "client": client,
    "signal_service": signal_service,
    "setting_repo": setting_repo,
    "notifier": notifier,
    "publisher": publisher,
    "trade_repo": trade_repo,
  }


def _webhook_body(**overrides):
  body = {
    "strategy": "s",
    "symbol": "OANDA:XAUUSD",
    "timeframe": "60",
    "timestamp": "2026-01-01T00:00:00Z",
    "position": {"action": "LONG", "price": 1.0, "quantity": 1.0},
    "token": "secret",
  }
  body.update(overrides)
  return body


# ── Health ──────────────────────────────────────────────────────────


def test_health_no_auth(ctx):
  resp = ctx["client"].get("/v1/health")
  assert resp.status_code == 200
  assert resp.json() == {"status": "ok"}


# ── Webhook ─────────────────────────────────────────────────────────


def test_webhook_accepts_valid_payload(ctx):
  resp = ctx["client"].post("/secret/webhook", json=_webhook_body())
  assert resp.status_code == 202
  assert resp.json()["status"] == "accepted"
  assert len(ctx["signal_service"].calls) == 1


def test_webhook_rejects_invalid_body(ctx):
  resp = ctx["client"].post("/secret/webhook", json={"foo": "bar"})
  assert resp.status_code == 422


def test_webhook_translates_signal_error(ctx):
  from broker.services.signal_processing_service import SignalError

  async def boom(_payload):
    raise SignalError(403, "blocked")

  ctx["signal_service"].process = boom
  resp = ctx["client"].post("/secret/webhook", json=_webhook_body())
  assert resp.status_code == 403
  assert resp.json()["detail"] == "blocked"


# ── Accounts ────────────────────────────────────────────────────────


def test_list_accounts(ctx):
  resp = ctx["client"].get("/v1/accounts", headers={"X-API-KEY": API_KEY})
  assert resp.status_code == 200
  data = resp.json()
  assert len(data) == 1
  assert data[0]["account_id"] == "acc-1"
  assert data[0]["market_type"] == "FOREX"


# ── Trades ──────────────────────────────────────────────────────────


def test_list_trades_default_pagination(ctx):
  resp = ctx["client"].get("/v1/acc-1/trades", headers={"X-API-KEY": API_KEY})
  assert resp.status_code == 200
  body = resp.json()
  assert body["page"]["total"] == 1
  assert body["page"]["limit"] == 20
  assert body["page"]["order"] == "desc"
  assert len(body["data"]) == 1
  assert body["data"][0]["symbol"] == "XAUUSD"


def test_list_trades_forwards_query_params(ctx):
  ctx["client"].get(
    "/v1/acc-1/trades?limit=5&offset=10&order=asc&order_by=symbol",
    headers={"X-API-KEY": API_KEY},
  )
  assert ctx["trade_repo"].list_kwargs == dict(
    account_id="acc-1", limit=5, offset=10, order="asc", order_by="symbol"
  )


def test_list_trades_rejects_out_of_range_limit(ctx):
  resp = ctx["client"].get("/v1/acc-1/trades?limit=0", headers={"X-API-KEY": API_KEY})
  assert resp.status_code == 422


def test_list_trades_rejects_invalid_order_by(ctx):
  resp = ctx["client"].get(
    "/v1/acc-1/trades?order_by=bogus", headers={"X-API-KEY": API_KEY}
  )
  assert resp.status_code == 422


# ── Admin settings toggles ─────────────────────────────────────────


def test_toggle_block_signal_from_unset_to_enabled(ctx):
  resp = ctx["client"].post(
    "/admin/settings/block-signal", headers={"X-API-KEY": API_KEY}
  )
  assert resp.status_code == 200
  body = resp.json()
  assert body["setting"] == SIGNAL_BLOCKED
  assert body["value"] == "1"
  assert body["state"] == "ENABLED"
  assert len(ctx["notifier"].messages) == 1


def test_toggle_block_signal_flips_back(ctx):
  ctx["setting_repo"].values[SIGNAL_BLOCKED] = "1"
  resp = ctx["client"].post(
    "/admin/settings/block-signal", headers={"X-API-KEY": API_KEY}
  )
  assert resp.json()["value"] == "0"
  assert resp.json()["state"] == "DISABLED"


def test_toggle_persist_failure_returns_500(ctx):
  ctx["setting_repo"].fail_set = True
  resp = ctx["client"].post(
    "/admin/settings/silent-signal", headers={"X-API-KEY": API_KEY}
  )
  assert resp.status_code == 500


def test_toggle_include_signal_raw(ctx):
  resp = ctx["client"].post(
    "/admin/settings/include-signal-raw", headers={"X-API-KEY": API_KEY}
  )
  assert resp.status_code == 200
  assert resp.json()["value"] == "1"


# ── Admin settings — crypto ─────────────────────────────────────────


def test_set_crypto_allowed_symbol(ctx):
  resp = ctx["client"].post(
    "/admin/settings/crypto-allowed-symbol",
    json={"symbols": ["btc", " eth ", "btc"]},
    headers={"X-API-KEY": API_KEY},
  )
  assert resp.status_code == 200
  body = resp.json()
  assert body["setting"] == CRYPTO_ALLOWED_SYMBOL_KEY
  # Normalised: upper-cased, trimmed, de-duplicated, order preserved.
  assert body["value"] == "BTC,ETH"
  assert ctx["setting_repo"].values[CRYPTO_ALLOWED_SYMBOL_KEY] == "BTC,ETH"
  assert len(ctx["notifier"].messages) == 1
  # No crypto_max_leverage set yet, so the broadcast is skipped (an incomplete
  # config is never pushed to workers).
  assert ctx["publisher"].system_signals == []


def test_set_crypto_allowed_symbol_broadcasts_when_leverage_present(ctx):
  ctx["setting_repo"].values[CRYPTO_MAX_LEVERAGE_KEY] = "10"

  resp = ctx["client"].post(
    "/admin/settings/crypto-allowed-symbol",
    json={"symbols": ["btc", " eth "]},
    headers={"X-API-KEY": API_KEY},
  )
  assert resp.status_code == 200

  # Both settings present -> CRYPTO_LEVERAGE_INIT broadcast to all crypto workers.
  assert len(ctx["publisher"].system_signals) == 1
  sig = ctx["publisher"].system_signals[0]
  assert sig["action"] == SystemActionEnum.CRYPTO_LEVERAGE_INIT
  assert sig["account_id"] == SYSTEM_BROADCAST_ACCOUNT_ID
  assert sig["symbols"] == ["BTC", "ETH"]
  assert sig["default_leverage"] == 10
  # A broadcast targets the shared SYSTEM subject, never a reply inbox.
  assert sig.get("subject") is None


def test_set_crypto_allowed_symbol_skips_broadcast_on_invalid_leverage(ctx):
  ctx["setting_repo"].values[CRYPTO_MAX_LEVERAGE_KEY] = "not-an-int"

  resp = ctx["client"].post(
    "/admin/settings/crypto-allowed-symbol",
    json={"symbols": ["BTC"]},
    headers={"X-API-KEY": API_KEY},
  )
  # The persist + response still succeed; only the live push is skipped.
  assert resp.status_code == 200
  assert ctx["setting_repo"].values[CRYPTO_ALLOWED_SYMBOL_KEY] == "BTC"
  assert ctx["publisher"].system_signals == []


def test_set_crypto_allowed_symbol_rejects_empty_list(ctx):
  resp = ctx["client"].post(
    "/admin/settings/crypto-allowed-symbol",
    json={"symbols": []},
    headers={"X-API-KEY": API_KEY},
  )
  assert resp.status_code == 422


def test_set_crypto_allowed_symbol_rejects_blank_only_symbols(ctx):
  resp = ctx["client"].post(
    "/admin/settings/crypto-allowed-symbol",
    json={"symbols": ["  ", ""]},
    headers={"X-API-KEY": API_KEY},
  )
  assert resp.status_code == 422


def test_set_crypto_allowed_symbol_persist_failure_returns_500(ctx):
  ctx["setting_repo"].fail_set = True
  resp = ctx["client"].post(
    "/admin/settings/crypto-allowed-symbol",
    json={"symbols": ["BTC"]},
    headers={"X-API-KEY": API_KEY},
  )
  assert resp.status_code == 500


def test_set_crypto_max_leverage(ctx):
  resp = ctx["client"].post(
    "/admin/settings/crypto-max-leverage",
    json={"default_leverage": 20},
    headers={"X-API-KEY": API_KEY},
  )
  assert resp.status_code == 200
  body = resp.json()
  assert body["setting"] == CRYPTO_MAX_LEVERAGE_KEY
  assert body["value"] == "20"
  assert ctx["setting_repo"].values[CRYPTO_MAX_LEVERAGE_KEY] == "20"
  # No crypto_allowed_symbol set yet, so the broadcast is skipped.
  assert ctx["publisher"].system_signals == []


def test_set_crypto_max_leverage_broadcasts_when_symbols_present(ctx):
  ctx["setting_repo"].values[CRYPTO_ALLOWED_SYMBOL_KEY] = "BTC,ETH"

  resp = ctx["client"].post(
    "/admin/settings/crypto-max-leverage",
    json={"default_leverage": 20},
    headers={"X-API-KEY": API_KEY},
  )
  assert resp.status_code == 200

  assert len(ctx["publisher"].system_signals) == 1
  sig = ctx["publisher"].system_signals[0]
  assert sig["action"] == SystemActionEnum.CRYPTO_LEVERAGE_INIT
  assert sig["account_id"] == SYSTEM_BROADCAST_ACCOUNT_ID
  assert sig["symbols"] == ["BTC", "ETH"]
  assert sig["default_leverage"] == 20
  assert sig.get("subject") is None


def test_set_crypto_max_leverage_rejects_zero(ctx):
  resp = ctx["client"].post(
    "/admin/settings/crypto-max-leverage",
    json={"default_leverage": 0},
    headers={"X-API-KEY": API_KEY},
  )
  assert resp.status_code == 422


def test_set_crypto_max_leverage_rejects_negative(ctx):
  resp = ctx["client"].post(
    "/admin/settings/crypto-max-leverage",
    json={"default_leverage": -10},
    headers={"X-API-KEY": API_KEY},
  )
  assert resp.status_code == 422


def test_set_crypto_max_leverage_persist_failure_returns_500(ctx):
  ctx["setting_repo"].fail_set = True
  resp = ctx["client"].post(
    "/admin/settings/crypto-max-leverage",
    json={"default_leverage": 10},
    headers={"X-API-KEY": API_KEY},
  )
  assert resp.status_code == 500


# ── Admin settings — notification timezone ──────────────────────────


def test_set_notification_timezone(ctx):
  resp = ctx["client"].post(
    "/admin/settings/notification-timezone",
    json={"utc_offset_hours": 9},
    headers={"X-API-KEY": API_KEY},
  )
  assert resp.status_code == 200
  body = resp.json()
  assert body["setting"] == NOTIFICATION_TIMEZONE_KEY
  assert body["value"] == "9"
  assert ctx["setting_repo"].values[NOTIFICATION_TIMEZONE_KEY] == "9"
  assert len(ctx["notifier"].messages) == 1


def test_set_notification_timezone_accepts_fractional_offset(ctx):
  resp = ctx["client"].post(
    "/admin/settings/notification-timezone",
    json={"utc_offset_hours": 5.5},
    headers={"X-API-KEY": API_KEY},
  )
  assert resp.status_code == 200
  assert resp.json()["value"] == "5.5"


def test_set_notification_timezone_rejects_above_range(ctx):
  resp = ctx["client"].post(
    "/admin/settings/notification-timezone",
    json={"utc_offset_hours": 15},
    headers={"X-API-KEY": API_KEY},
  )
  assert resp.status_code == 422


def test_set_notification_timezone_rejects_below_range(ctx):
  resp = ctx["client"].post(
    "/admin/settings/notification-timezone",
    json={"utc_offset_hours": -13},
    headers={"X-API-KEY": API_KEY},
  )
  assert resp.status_code == 422


def test_set_notification_timezone_persist_failure_returns_500(ctx):
  ctx["setting_repo"].fail_set = True
  resp = ctx["client"].post(
    "/admin/settings/notification-timezone",
    json={"utc_offset_hours": 7},
    headers={"X-API-KEY": API_KEY},
  )
  assert resp.status_code == 500


# ── Admin flat ──────────────────────────────────────────────────────


def test_flat_all_scope(ctx):
  resp = ctx["client"].post("/admin/flat", json={}, headers={"X-API-KEY": API_KEY})
  assert resp.status_code == 200
  body = resp.json()
  assert body["action"] == "FLAT"
  assert body["scope"] == "ALL"
  assert len(ctx["publisher"].admin_signals) == 1


def test_flat_scoped(ctx):
  resp = ctx["client"].post(
    "/admin/flat",
    json={"strategy": "s", "symbol": "XAUUSD"},
    headers={"X-API-KEY": API_KEY},
  )
  body = resp.json()
  assert "strategy=s" in body["scope"]
  assert "symbol=XAUUSD" in body["scope"]
  published = ctx["publisher"].admin_signals[0]
  assert published["strategy"] == "s"
  assert published["symbol"] == "XAUUSD"


# ── Auth enforcement (ensure_api_key NOT overridden) ───────────────


@pytest.fixture
def auth_client():
  """App where ensure_api_key is enforced (settings.BROKER_API_KEY is
  'test-api-key' from conftest)."""
  app = FastAPI()
  app.include_router(get_core_router())
  app.dependency_overrides[get_account_repository] = lambda: FakeAccountRepo([])
  app.dependency_overrides[get_trade_repository] = lambda: FakeTradeRepo([], 0)
  app.dependency_overrides[get_setting_repository] = lambda: FakeSettingRepo()
  app.dependency_overrides[get_admin_notifier] = lambda: FakeNotifier()
  app.dependency_overrides[get_publisher] = lambda: FakePublisher()
  return TestClient(app)


def test_accounts_requires_api_key(auth_client):
  assert auth_client.get("/v1/accounts").status_code == 401


def test_accounts_rejects_wrong_api_key(auth_client):
  resp = auth_client.get("/v1/accounts", headers={"X-API-KEY": "wrong"})
  assert resp.status_code == 401


def test_accounts_accepts_correct_api_key(auth_client):
  resp = auth_client.get("/v1/accounts", headers={"X-API-KEY": API_KEY})
  assert resp.status_code == 200


def test_admin_requires_api_key(auth_client):
  assert auth_client.post("/admin/settings/block-signal").status_code == 401
