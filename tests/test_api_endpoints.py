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

from broker.app import install_webhook_connection_close
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
from broker.schemas.account_schema import MarketTypeEnum, compose_worker_id
from broker.schemas.core import SignalActionEnum
from broker.schemas.publisher_schema import SystemActionEnum
from broker.security.ensure_api_key import ensure_api_key

API_KEY = "test-api-key"


# ── Fakes ───────────────────────────────────────────────────────────


class FakeSignalService:
  def __init__(self):
    self.calls = []

  async def enqueue(self, payload):
    self.calls.append(payload)
    return {
      "status": "queued",
      "timestamp": payload.timestamp.isoformat(),
    }

  # ``enqueue`` is what the webhook route calls now; ``process`` is kept as an
  # alias on the real service for back-compat and remains available here so
  # tests can still monkey-patch ``ctx["signal_service"].process``.
  process = enqueue


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
    self.accounts = list(accounts)

  async def get_all(self):
    return self.accounts

  async def get_by_market(self, market):
    return [a for a in self.accounts if a.market_type == market]

  async def create_account(self, account_id, market, gateway, account_name=None):
    # account_id alone isn't unique — only the full (market, gateway, account_id)
    # triple is (see uq_accounts_market_gateway_account_id).
    if any(
      a.account_id == account_id and a.market_type == market and a.gateway == gateway
      for a in self.accounts
    ):
      return None
    account = _make_account(
      account_id=account_id, account_name=account_name, market_type=market, gateway=gateway
    )
    self.accounts.append(account)
    return account


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


def _make_account(
  *,
  account_id="acc-1",
  account_name="Main",
  market_type=MarketTypeEnum.FOREX,
  gateway=None,
) -> Account:
  return Account(
    id=uuid.uuid4(),
    account_id=account_id,
    account_name=account_name,
    account_balance=1000.0,
    market_type=market_type,
    gateway=gateway,
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
  install_webhook_connection_close(app)
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
    "account_repo": account_repo,
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
  # The webhook is a fast enqueue-only path now — the response signals the
  # signal is queued on JetStream, not that the full pipeline succeeded.
  assert resp.json()["status"] == "queued"
  assert len(ctx["signal_service"].calls) == 1


def test_webhook_rejects_invalid_body(ctx):
  resp = ctx["client"].post("/secret/webhook", json={"foo": "bar"})
  assert resp.status_code == 422


def test_webhook_translates_signal_error(ctx):
  from broker.services.signal_processing_service import SignalError

  async def boom(_payload):
    raise SignalError(401, "bad token")

  ctx["signal_service"].enqueue = boom
  resp = ctx["client"].post("/secret/webhook", json=_webhook_body())
  assert resp.status_code == 401
  assert resp.json()["detail"] == "bad token"


def test_webhook_response_forces_connection_close(ctx):
  """TradingView must not reuse the pooled socket between alerts."""
  resp = ctx["client"].post("/secret/webhook", json=_webhook_body())
  assert resp.status_code == 202
  assert resp.headers.get("connection", "").lower() == "close"


def test_webhook_error_response_forces_connection_close(ctx):
  """Same guarantee on the error path — otherwise TradingView would still
  pool a socket the server intended to close after the response."""
  resp = ctx["client"].post("/secret/webhook", json={"foo": "bar"})
  assert resp.status_code == 422
  assert resp.headers.get("connection", "").lower() == "close"


def test_non_webhook_endpoint_keeps_default_connection(ctx):
  """Middleware must be scoped to /secret/webhook — other routes stay on
  keep-alive so admin/API callers still benefit from connection reuse."""
  resp = ctx["client"].get("/v1/health")
  assert resp.status_code == 200
  assert resp.headers.get("connection", "").lower() != "close"


# ── Accounts ────────────────────────────────────────────────────────


def test_list_accounts(ctx):
  resp = ctx["client"].get("/v1/accounts", headers={"X-API-KEY": API_KEY})
  assert resp.status_code == 200
  data = resp.json()
  assert len(data) == 1
  assert data[0]["account_id"] == "acc-1"
  assert data[0]["market_type"] == "FOREX"


# ── Create account (admin) ─────────────────────────────────────────


def test_create_account_success(ctx):
  resp = ctx["client"].post(
    "/admin/accounts",
    json={"market_type": "CRYPTO", "gateway": "BINANCE", "account_id": "7654321"},
    headers={"X-API-KEY": API_KEY},
  )
  assert resp.status_code == 201
  body = resp.json()
  assert body["account_id"] == "7654321"
  assert body["market_type"] == "CRYPTO"
  assert body["gateway"] == "BINANCE"


def test_create_account_rejects_invalid_gateway_for_market(ctx):
  resp = ctx["client"].post(
    "/admin/accounts",
    json={"market_type": "FOREX", "gateway": "BINANCE", "account_id": "7654321"},
    headers={"X-API-KEY": API_KEY},
  )
  assert resp.status_code == 422


def test_create_account_conflict_on_duplicate_id(ctx):
  # Seed account "acc-1" already has market/gateway set — same triple posted
  # again should conflict.
  ctx["account_repo"].accounts[0].gateway = "MT5"
  resp = ctx["client"].post(
    "/admin/accounts",
    json={"market_type": "FOREX", "gateway": "MT5", "account_id": "acc-1"},
    headers={"X-API-KEY": API_KEY},
  )
  assert resp.status_code == 409


def test_create_account_allows_same_account_id_on_different_gateway(ctx):
  # "acc-1" already exists as a FOREX/MT5 account; the same bare account_id
  # under a different market/gateway is a distinct account and must succeed.
  ctx["account_repo"].accounts[0].gateway = "MT5"
  resp = ctx["client"].post(
    "/admin/accounts",
    json={"market_type": "CRYPTO", "gateway": "BINANCE", "account_id": "acc-1"},
    headers={"X-API-KEY": API_KEY},
  )
  assert resp.status_code == 201
  assert resp.json()["gateway"] == "BINANCE"


def test_create_account_rejects_colon_in_account_id(ctx):
  resp = ctx["client"].post(
    "/admin/accounts",
    json={"market_type": "FOREX", "gateway": "MT5", "account_id": "bad:id"},
    headers={"X-API-KEY": API_KEY},
  )
  assert resp.status_code == 422


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
  # No crypto_max_leverage set yet, so the push is skipped (an incomplete
  # config is never sent to workers).
  assert ctx["publisher"].system_signals == []


def test_set_crypto_allowed_symbol_pushes_per_crypto_account(ctx):
  ctx["setting_repo"].values[CRYPTO_MAX_LEVERAGE_KEY] = "10"
  # Two crypto accounts (targeted) plus the default forex one (ignored).
  ctx["account_repo"].accounts.append(
    _make_account(
      account_id="7654321", market_type=MarketTypeEnum.CRYPTO, gateway="BINANCE"
    )
  )
  ctx["account_repo"].accounts.append(
    _make_account(account_id="111", market_type=MarketTypeEnum.CRYPTO, gateway="BYBIT")
  )

  resp = ctx["client"].post(
    "/admin/settings/crypto-allowed-symbol",
    json={"symbols": ["btc", " eth "]},
    headers={"X-API-KEY": API_KEY},
  )
  assert resp.status_code == 200

  # One CRYPTO_LEVERAGE_INIT per crypto account, addressed to its composite id.
  signals = ctx["publisher"].system_signals
  assert len(signals) == 2
  targets = {s["account_id"] for s in signals}
  assert targets == {"CRYPTO-BINANCE-7654321", "CRYPTO-BYBIT-111"}
  for sig in signals:
    assert sig["action"] == SystemActionEnum.CRYPTO_LEVERAGE_INIT
    assert sig["symbols"] == ["BTC", "ETH"]
    assert sig["default_leverage"] == 10
    # Sent on the shared SYSTEM subject, never a reply inbox.
    assert sig.get("subject") is None


def test_set_crypto_allowed_symbol_skips_crypto_account_without_gateway(ctx):
  ctx["setting_repo"].values[CRYPTO_MAX_LEVERAGE_KEY] = "10"
  # Crypto account whose gateway was never reported cannot be addressed.
  ctx["account_repo"].accounts.append(
    _make_account(account_id="999", market_type=MarketTypeEnum.CRYPTO, gateway=None)
  )

  resp = ctx["client"].post(
    "/admin/settings/crypto-allowed-symbol",
    json={"symbols": ["BTC"]},
    headers={"X-API-KEY": API_KEY},
  )
  assert resp.status_code == 200
  assert ctx["publisher"].system_signals == []


def test_set_crypto_allowed_symbol_skips_push_on_invalid_leverage(ctx):
  ctx["setting_repo"].values[CRYPTO_MAX_LEVERAGE_KEY] = "not-an-int"
  ctx["account_repo"].accounts.append(
    _make_account(
      account_id="7654321", market_type=MarketTypeEnum.CRYPTO, gateway="BINANCE"
    )
  )

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
  # No crypto_allowed_symbol set yet, so the push is skipped.
  assert ctx["publisher"].system_signals == []


def test_set_crypto_max_leverage_pushes_per_crypto_account(ctx):
  ctx["setting_repo"].values[CRYPTO_ALLOWED_SYMBOL_KEY] = "BTC,ETH"
  ctx["account_repo"].accounts.append(
    _make_account(
      account_id="7654321", market_type=MarketTypeEnum.CRYPTO, gateway="BINANCE"
    )
  )

  resp = ctx["client"].post(
    "/admin/settings/crypto-max-leverage",
    json={"default_leverage": 20},
    headers={"X-API-KEY": API_KEY},
  )
  assert resp.status_code == 200

  assert len(ctx["publisher"].system_signals) == 1
  sig = ctx["publisher"].system_signals[0]
  assert sig["action"] == SystemActionEnum.CRYPTO_LEVERAGE_INIT
  assert sig["account_id"] == compose_worker_id(
    MarketTypeEnum.CRYPTO, "BINANCE", "7654321"
  )
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


# ── Admin settings (read) ──────────────────────────────────────────


def test_get_settings_defaults_disabled(ctx):
  resp = ctx["client"].get("/admin/settings", headers={"X-API-KEY": API_KEY})
  assert resp.status_code == 200
  body = resp.json()
  assert len(body) == 3
  assert all(item["value"] == "0" and item["state"] == "DISABLED" for item in body)


def test_get_settings_reflects_enabled(ctx):
  ctx["setting_repo"].values[SIGNAL_BLOCKED] = "1"
  resp = ctx["client"].get("/admin/settings", headers={"X-API-KEY": API_KEY})
  blocked = next(i for i in resp.json() if i["setting"] == SIGNAL_BLOCKED)
  assert blocked["value"] == "1"
  assert blocked["state"] == "ENABLED"


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


def test_flat_scoped_by_market_and_gateway(ctx):
  resp = ctx["client"].post(
    "/admin/flat",
    json={"account_id": "acc-1", "market_type": "CRYPTO", "gateway": "BINANCE"},
    headers={"X-API-KEY": API_KEY},
  )
  body = resp.json()
  assert "market=CRYPTO" in body["scope"]
  assert "gateway=BINANCE" in body["scope"]
  published = ctx["publisher"].admin_signals[0]
  assert published["market_type"] == MarketTypeEnum.CRYPTO
  assert published["gateway"] == "BINANCE"


def test_flat_rejects_account_id_without_market_and_gateway(ctx):
  resp = ctx["client"].post(
    "/admin/flat",
    json={"account_id": "acc-1"},
    headers={"X-API-KEY": API_KEY},
  )
  assert resp.status_code == 422
  assert ctx["publisher"].admin_signals == []


def test_flat_rejects_account_id_with_only_gateway(ctx):
  resp = ctx["client"].post(
    "/admin/flat",
    json={"account_id": "acc-1", "gateway": "MT5"},
    headers={"X-API-KEY": API_KEY},
  )
  assert resp.status_code == 422


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
