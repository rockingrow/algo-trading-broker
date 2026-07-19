"""HTTP tests for the ``/v1/telegram/*`` endpoints consumed by the bot.

Assembled from the real routers with every infrastructure dependency replaced
by in-memory fakes via ``dependency_overrides`` (no DB / NATS needed).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from broker.db.models import Account, Trade
from broker.providers import (
  get_account_repository,
  get_publisher,
  get_trade_repository,
)
from broker.router import get_core_router
from broker.schemas.account_schema import MarketTypeEnum
from broker.schemas.core import SignalActionEnum
from broker.schemas.trade_schema import TradeStatusEnum
from broker.security.ensure_api_key import ensure_api_key

API_KEY = "test-api-key"
TG_ID = 4242
TOKEN = uuid.uuid4()


class FakeTgAccountRepo:
  """In-memory stand-in for ``AccountRepository`` supporting several accounts
  per telegram_user_id, mirroring ``SqlAlchemyAccountRepository``'s
  active-account model: the first account linked becomes active; later links
  don't disturb it."""

  def __init__(self, accounts: list[Account] | Account | None):
    if accounts is None:
      accounts = []
    elif isinstance(accounts, Account):
      accounts = [accounts]
    self._by_token = {str(a.telegram_link_token): a for a in accounts}
    self._linked: dict[int, list[Account]] = {}
    self._active: dict[int, uuid.UUID] = {}

  async def list_by_telegram_user_id(self, telegram_user_id):
    return list(self._linked.get(telegram_user_id, []))

  async def get_active_account(self, telegram_user_id):
    linked = self._linked.get(telegram_user_id)
    if not linked:
      return None
    active_id = self._active.get(telegram_user_id)
    return next((a for a in linked if a.id == active_id), linked[0])

  async def set_active_account(self, telegram_user_id, account_id):
    linked = self._linked.get(telegram_user_id, [])
    match = next((a for a in linked if a.id == account_id), None)
    if match is None:
      return None
    self._active[telegram_user_id] = account_id
    return match

  async def link_telegram(self, token, telegram_user_id):
    account = self._by_token.get(str(token))
    if account is None:
      return None
    account.telegram_user_id = telegram_user_id
    linked = self._linked.setdefault(telegram_user_id, [])
    if account not in linked:
      linked.append(account)
    self._active.setdefault(telegram_user_id, account.id)
    return account

  async def unlink_telegram(self, telegram_user_id):
    linked = self._linked.get(telegram_user_id)
    if not linked:
      return False
    active = await self.get_active_account(telegram_user_id)
    linked.remove(active)
    active.telegram_user_id = None
    if linked:
      self._active[telegram_user_id] = linked[0].id
    else:
      self._active.pop(telegram_user_id, None)
    return True


class FakeTradeRepo:
  def __init__(self, trades, total):
    self._trades = trades
    self._total = total

  async def list_by_account(self, account_id, *, limit, offset, order, order_by):
    return self._trades

  async def count_by_account(self, account_id):
    return self._total


class FakePublisher:
  def __init__(self):
    self.admin_signals = []

  async def publish_admin_signal(self, **kwargs):
    self.admin_signals.append(kwargs)


def _make_account() -> Account:
  return Account(
    id=uuid.uuid4(),
    account_id="acc-1",
    account_name="Main",
    account_balance=1000.0,
    market=MarketTypeEnum.FOREX,
    last_activity_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    telegram_user_id=None,
    telegram_link_token=TOKEN,
  )


def _make_trade() -> Trade:
  return Trade(
    id=uuid.uuid4(),
    account_id="acc-1",
    account_leverage=100,
    account_balance_init=1000.0,
    account_balance=1010.0,
    ref_id="r-1",
    strategy="strat",
    strategy_code="LONG|sig",
    symbol="XAUUSD",
    action=SignalActionEnum.LONG,
    price=100.0,
    quantity=1.0,
    is_running=True,
    risk_percent=1.0,
    status=TradeStatusEnum.OPENED,
    createdAt=datetime(2026, 1, 1, tzinfo=timezone.utc),
    updatedAt=datetime(2026, 1, 1, tzinfo=timezone.utc),
  )


@pytest.fixture
def ctx():
  app = FastAPI()
  app.include_router(get_core_router())

  account_repo = FakeTgAccountRepo(_make_account())
  trade_repo = FakeTradeRepo([_make_trade()], total=1)
  publisher = FakePublisher()

  app.dependency_overrides[get_account_repository] = lambda: account_repo
  app.dependency_overrides[get_trade_repository] = lambda: trade_repo
  app.dependency_overrides[get_publisher] = lambda: publisher
  app.dependency_overrides[ensure_api_key] = lambda: None

  return {
    "client": TestClient(app),
    "account_repo": account_repo,
    "publisher": publisher,
  }


def _headers():
  return {"X-API-KEY": API_KEY}


# ── link ────────────────────────────────────────────────────────────


def test_link_success(ctx):
  resp = ctx["client"].post(
    "/v1/telegram/link",
    json={"token": str(TOKEN), "telegram_user_id": TG_ID},
    headers=_headers(),
  )
  assert resp.status_code == 200
  body = resp.json()
  assert body["account_id"] == "acc-1"
  assert body["telegram_user_id"] == TG_ID


def test_link_invalid_token(ctx):
  resp = ctx["client"].post(
    "/v1/telegram/link",
    json={"token": str(uuid.uuid4()), "telegram_user_id": TG_ID},
    headers=_headers(),
  )
  assert resp.status_code == 404


# ── resolve / unlink ────────────────────────────────────────────────


def test_get_account_not_linked(ctx):
  resp = ctx["client"].get(f"/v1/telegram/{TG_ID}", headers=_headers())
  assert resp.status_code == 404


def test_get_account_after_link(ctx):
  ctx["client"].post(
    "/v1/telegram/link",
    json={"token": str(TOKEN), "telegram_user_id": TG_ID},
    headers=_headers(),
  )
  resp = ctx["client"].get(f"/v1/telegram/{TG_ID}", headers=_headers())
  assert resp.status_code == 200
  assert resp.json()["account_id"] == "acc-1"


def test_unlink_after_link(ctx):
  ctx["client"].post(
    "/v1/telegram/link",
    json={"token": str(TOKEN), "telegram_user_id": TG_ID},
    headers=_headers(),
  )
  resp = ctx["client"].post(f"/v1/telegram/{TG_ID}/unlink", headers=_headers())
  assert resp.status_code == 200
  assert resp.json()["status"] == "unlinked"


def test_unlink_not_linked(ctx):
  resp = ctx["client"].post(f"/v1/telegram/{TG_ID}/unlink", headers=_headers())
  assert resp.status_code == 404


# ── trades ──────────────────────────────────────────────────────────


def test_trades_requires_link(ctx):
  resp = ctx["client"].get(f"/v1/telegram/{TG_ID}/trades", headers=_headers())
  assert resp.status_code == 404


def test_trades_after_link(ctx):
  ctx["client"].post(
    "/v1/telegram/link",
    json={"token": str(TOKEN), "telegram_user_id": TG_ID},
    headers=_headers(),
  )
  resp = ctx["client"].get(f"/v1/telegram/{TG_ID}/trades", headers=_headers())
  assert resp.status_code == 200
  body = resp.json()
  assert body["page"]["total"] == 1
  assert body["data"][0]["symbol"] == "XAUUSD"


# ── commands ────────────────────────────────────────────────────────


def _link(ctx):
  ctx["client"].post(
    "/v1/telegram/link",
    json={"token": str(TOKEN), "telegram_user_id": TG_ID},
    headers=_headers(),
  )


def test_flat_publishes_scoped_to_account(ctx):
  _link(ctx)
  resp = ctx["client"].post(
    f"/v1/telegram/{TG_ID}/commands/flat", json={}, headers=_headers()
  )
  assert resp.status_code == 200
  assert resp.json()["action"] == "FLAT"
  published = ctx["publisher"].admin_signals[-1]
  assert published["action"].value == "FLAT"
  assert published["account_id"] == "acc-1"
  # market/gateway ride along so a worker that checks them can
  # disambiguate account_id reused across gateways.
  assert published["market"] == MarketTypeEnum.FOREX
  assert published["gateway"] is None


def test_prevent_block_publishes_block_entries(ctx):
  _link(ctx)
  resp = ctx["client"].post(
    f"/v1/telegram/{TG_ID}/commands/prevent",
    json={"enabled": True},
    headers=_headers(),
  )
  assert resp.status_code == 200
  assert resp.json()["action"] == "BLOCK_ENTRIES"
  assert ctx["publisher"].admin_signals[-1]["action"].value == "BLOCK_ENTRIES"


def test_prevent_allow_publishes_allow_entries(ctx):
  _link(ctx)
  resp = ctx["client"].post(
    f"/v1/telegram/{TG_ID}/commands/prevent",
    json={"enabled": False},
    headers=_headers(),
  )
  assert resp.status_code == 200
  assert resp.json()["action"] == "ALLOW_ENTRIES"


def test_command_requires_link(ctx):
  resp = ctx["client"].post(
    f"/v1/telegram/{TG_ID}/commands/flat", json={}, headers=_headers()
  )
  assert resp.status_code == 404


# ── accounts list / active-account switch ──────────────────────────


TOKEN_2 = uuid.uuid4()


def _make_account2() -> Account:
  return Account(
    id=uuid.uuid4(),
    account_id="acc-2",
    account_name="Second",
    account_balance=500.0,
    market=MarketTypeEnum.CRYPTO,
    gateway="BINANCE",
    last_activity_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    telegram_user_id=None,
    telegram_link_token=TOKEN_2,
  )


@pytest.fixture
def multi_ctx():
  app = FastAPI()
  app.include_router(get_core_router())

  account_repo = FakeTgAccountRepo([_make_account(), _make_account2()])
  app.dependency_overrides[get_account_repository] = lambda: account_repo
  app.dependency_overrides[get_trade_repository] = lambda: FakeTradeRepo([], 0)
  app.dependency_overrides[get_publisher] = lambda: FakePublisher()
  app.dependency_overrides[ensure_api_key] = lambda: None

  return {"client": TestClient(app), "account_repo": account_repo}


def test_list_accounts_empty_when_not_linked(ctx):
  resp = ctx["client"].get(f"/v1/telegram/{TG_ID}/accounts", headers=_headers())
  assert resp.status_code == 200
  assert resp.json() == []


def test_list_accounts_after_link(ctx):
  _link(ctx)
  resp = ctx["client"].get(f"/v1/telegram/{TG_ID}/accounts", headers=_headers())
  assert resp.status_code == 200
  body = resp.json()
  assert len(body) == 1
  assert body[0]["account_id"] == "acc-1"
  assert body[0]["is_active"] is True


def _link_both(ctx):
  ctx["client"].post(
    "/v1/telegram/link",
    json={"token": str(TOKEN), "telegram_user_id": TG_ID},
    headers=_headers(),
  )
  ctx["client"].post(
    "/v1/telegram/link",
    json={"token": str(TOKEN_2), "telegram_user_id": TG_ID},
    headers=_headers(),
  )


def test_second_link_does_not_disturb_active_account(multi_ctx):
  _link_both(multi_ctx)
  resp = multi_ctx["client"].get(f"/v1/telegram/{TG_ID}/accounts", headers=_headers())
  body = resp.json()
  assert len(body) == 2
  active = [a for a in body if a["is_active"]]
  assert len(active) == 1
  assert active[0]["account_id"] == "acc-1"  # first-linked stays active

  # single-account endpoints still resolve to the active one
  resp = multi_ctx["client"].get(f"/v1/telegram/{TG_ID}", headers=_headers())
  assert resp.json()["account_id"] == "acc-1"


def test_switch_active_account(multi_ctx):
  _link_both(multi_ctx)
  accounts = multi_ctx["client"].get(
    f"/v1/telegram/{TG_ID}/accounts", headers=_headers()
  ).json()
  second_id = next(a["id"] for a in accounts if a["account_id"] == "acc-2")

  resp = multi_ctx["client"].post(
    f"/v1/telegram/{TG_ID}/active-account",
    json={"account_id": second_id},
    headers=_headers(),
  )
  assert resp.status_code == 200
  assert resp.json()["account_id"] == "acc-2"
  assert resp.json()["is_active"] is True

  resp = multi_ctx["client"].get(f"/v1/telegram/{TG_ID}", headers=_headers())
  assert resp.json()["account_id"] == "acc-2"


def test_switch_active_account_not_owned(multi_ctx):
  resp = multi_ctx["client"].post(
    f"/v1/telegram/{TG_ID}/active-account",
    json={"account_id": str(uuid.uuid4())},
    headers=_headers(),
  )
  assert resp.status_code == 404


# ── auth enforcement ────────────────────────────────────────────────


def test_telegram_requires_api_key():
  app = FastAPI()
  app.include_router(get_core_router())
  app.dependency_overrides[get_account_repository] = lambda: FakeTgAccountRepo(None)
  app.dependency_overrides[get_trade_repository] = lambda: FakeTradeRepo([], 0)
  app.dependency_overrides[get_publisher] = lambda: FakePublisher()
  client = TestClient(app)
  assert client.get(f"/v1/telegram/{TG_ID}").status_code == 401
