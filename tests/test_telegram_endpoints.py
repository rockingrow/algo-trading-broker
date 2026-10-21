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

from broker.constants import DEFAULT_MAX_OPEN_ORDERS, MAX_OPEN_ORDERS_KEY
from broker.db.models import Account, Trade
from broker.providers import (
  get_account_repository,
  get_publisher,
  get_setting_repository,
  get_trade_broadcast_repository,
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
  """In-memory stand-in for ``AccountRepository`` mirroring
  ``SqlAlchemyAccountRepository``'s model: ``account_bot_links`` is a
  many-to-many set of ``(platform_user_id, account)`` pairs, tokens live apart
  from accounts, and the first account a user links becomes active while later
  links don't disturb it.

  ``_links`` is keyed by the platform user id **as a string**, matching the
  ``String(64)`` column the real repository writes."""

  def __init__(self, accounts: list[Account] | Account | None):
    if accounts is None:
      accounts = []
    elif isinstance(accounts, Account):
      accounts = [accounts]
    self._accounts = accounts
    # Tokens are no longer a column on the account, so they're a side map here
    # too. Seeded positionally: first account gets TOKEN, second TOKEN_2.
    self._by_token = dict(zip([str(TOKEN), str(TOKEN_2)], accounts))
    self._links: dict[str, list[Account]] = {}
    self._active: dict[str, uuid.UUID] = {}

  async def list_by_telegram_user_id(self, telegram_user_id, platform=None):
    return list(self._links.get(str(telegram_user_id), []))

  async def get_active_account(self, telegram_user_id, platform=None):
    linked = self._links.get(str(telegram_user_id))
    if not linked:
      return None
    active_id = self._active.get(str(telegram_user_id))
    return next((a for a in linked if a.id == active_id), linked[0])

  async def set_active_account(self, telegram_user_id, account_id, platform=None):
    linked = self._links.get(str(telegram_user_id), [])
    match = next((a for a in linked if a.id == account_id), None)
    if match is None:
      return None
    self._active[str(telegram_user_id)] = account_id
    return match

  async def link_telegram(self, token, telegram_user_id, platform=None):
    account = self._by_token.get(str(token))
    if account is None:
      return None
    key = str(telegram_user_id)
    linked = self._links.setdefault(key, [])
    if account not in linked:
      linked.append(account)
    self._active.setdefault(key, account.id)
    return account

  async def unlink_telegram(self, telegram_user_id, platform=None):
    key = str(telegram_user_id)
    linked = self._links.get(key)
    if not linked:
      return False
    active = await self.get_active_account(telegram_user_id)
    linked.remove(active)
    if linked:
      self._active[key] = linked[0].id
    else:
      self._active.pop(key, None)
    return True

  async def update_settings(self, account_uuid, patch):
    """Merge *patch* into the account's settings, like the real repository's
    server-side ``settings || patch``. ``None`` for an unknown row."""
    account = next((a for a in self._accounts if a.id == account_uuid), None)
    if account is None:
      return None
    merged = {**(account.settings or {}), **patch}
    account.settings = merged
    return merged

  def linked_user_ids(self, account: Account) -> list[str]:
    """Test helper: who currently holds *account* (the join table's other
    direction, which no endpoint exposes)."""
    return [uid for uid, accounts in self._links.items() if account in accounts]


class FakeTradeRepo:
  def __init__(self, trades, total, open_trades=None):
    self._trades = trades
    self._total = total
    self._open_trades = open_trades if open_trades is not None else trades

  async def list_by_account(self, account_id, *, limit, offset, order, order_by):
    return self._trades

  async def count_by_account(self, account_id):
    return self._total

  async def list_open_by_account(self, account_id):
    return self._open_trades


class FakePublisher:
  def __init__(self):
    self.admin_signals = []
    self.settings_updates = []

  async def publish_admin_signal(self, **kwargs):
    self.admin_signals.append(kwargs)

  async def publish_system_settings_update(self, **kwargs):
    self.settings_updates.append(kwargs)


class FakeSettingRepo:
  """Just enough of the broker-wide settings for the endpoints that read a
  global default (``max_open_orders``)."""

  def __init__(self, values=None):
    self.values = dict(values or {})

  async def get(self, key):
    return self.values.get(key)

  async def get_many(self, keys):
    return {k: v for k, v in self.values.items() if k in keys and v is not None}

  async def set(self, key, value):
    self.values[key] = value
    return True


def _make_account() -> Account:
  return Account(
    id=uuid.uuid4(),
    account_id="acc-1",
    account_name="Main",
    account_balance=1000.0,
    market=MarketTypeEnum.FOREX,
    last_activity_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
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
  setting_repo = FakeSettingRepo()

  app.dependency_overrides[get_account_repository] = lambda: account_repo
  app.dependency_overrides[get_trade_repository] = lambda: trade_repo
  app.dependency_overrides[get_publisher] = lambda: publisher
  app.dependency_overrides[get_setting_repository] = lambda: setting_repo
  app.dependency_overrides[ensure_api_key] = lambda: None

  return {
    "client": TestClient(app),
    "account_repo": account_repo,
    "publisher": publisher,
    "setting_repo": setting_repo,
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
  assert body["is_active"] is True
  # The response deliberately no longer echoes the caller's own id.
  assert "telegram_user_id" not in body


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


# ── positions ───────────────────────────────────────────────────────


def test_positions_requires_link(ctx):
  resp = ctx["client"].get(f"/v1/telegram/{TG_ID}/positions", headers=_headers())
  assert resp.status_code == 404


def test_positions_after_link(ctx):
  ctx["client"].post(
    "/v1/telegram/link",
    json={"token": str(TOKEN), "telegram_user_id": TG_ID},
    headers=_headers(),
  )
  resp = ctx["client"].get(f"/v1/telegram/{TG_ID}/positions", headers=_headers())
  assert resp.status_code == 200
  body = resp.json()
  assert body["count"] == 1
  assert body["data"][0]["symbol"] == "XAUUSD"


def test_positions_empty_when_none_open():
  app = FastAPI()
  app.include_router(get_core_router())
  account_repo = FakeTgAccountRepo(_make_account())
  app.dependency_overrides[get_account_repository] = lambda: account_repo
  app.dependency_overrides[get_trade_repository] = lambda: FakeTradeRepo(
    [_make_trade()], total=1, open_trades=[]
  )
  app.dependency_overrides[get_publisher] = lambda: FakePublisher()
  app.dependency_overrides[ensure_api_key] = lambda: None
  client = TestClient(app)

  client.post(
    "/v1/telegram/link",
    json={"token": str(TOKEN), "telegram_user_id": TG_ID},
    headers=_headers(),
  )
  resp = client.get(f"/v1/telegram/{TG_ID}/positions", headers=_headers())
  assert resp.status_code == 200
  body = resp.json()
  assert body["count"] == 0
  assert body["data"] == []


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


def test_prevent_block_publishes_block_signal(ctx):
  _link(ctx)
  resp = ctx["client"].post(
    f"/v1/telegram/{TG_ID}/commands/prevent",
    json={"enabled": True},
    headers=_headers(),
  )
  assert resp.status_code == 200
  assert resp.json()["action"] == "BLOCK_SIGNAL"
  assert ctx["publisher"].admin_signals[-1]["action"].value == "BLOCK_SIGNAL"


def test_prevent_allow_publishes_allow_signal(ctx):
  _link(ctx)
  resp = ctx["client"].post(
    f"/v1/telegram/{TG_ID}/commands/prevent",
    json={"enabled": False},
    headers=_headers(),
  )
  assert resp.status_code == 200
  assert resp.json()["action"] == "ALLOW_SIGNAL"


# ── commands persist into accounts.settings ─────────────────────────
#
# The ADMIN publish only reaches a worker that is connected right now; the
# settings blob is what the WORKER_CONNECTED_ACK replays on every reconnect,
# so a command that publishes without persisting silently expires.


def _prevent(ctx, enabled: bool):
  return ctx["client"].post(
    f"/v1/telegram/{TG_ID}/commands/prevent",
    json={"enabled": enabled},
    headers=_headers(),
  )


def test_prevent_persists_signal_blocked_on_the_account(ctx):
  _link(ctx)
  resp = _prevent(ctx, True)

  assert resp.status_code == 200
  assert resp.json()["settings"] == {"signal_blocked": True}
  assert ctx["account_repo"]._accounts[0].settings == {"signal_blocked": True}


def test_allow_flips_the_persisted_setting_back(ctx):
  _link(ctx)
  _prevent(ctx, True)
  resp = _prevent(ctx, False)

  assert resp.json()["settings"] == {"signal_blocked": False}
  assert ctx["account_repo"]._accounts[0].settings == {"signal_blocked": False}


def test_prevent_keeps_unrelated_settings_keys(ctx):
  _link(ctx)
  # A key this broker version doesn't know (written by a newer one, or by a
  # command added later) must survive the write — it is a merge, not a replace.
  ctx["account_repo"]._accounts[0].settings = {"future_toggle": "keep-me"}
  resp = _prevent(ctx, True)

  assert resp.json()["settings"] == {
    "future_toggle": "keep-me",
    "signal_blocked": True,
  }


def test_prevent_fails_without_publishing_when_the_write_fails(ctx):
  _link(ctx)

  async def failing_update(account_uuid, patch):
    return None

  ctx["account_repo"].update_settings = failing_update
  resp = _prevent(ctx, True)

  # A command whose intent could not be recorded is a failed command: nothing
  # is published either, so the worker and the DB can't disagree.
  assert resp.status_code == 500
  assert ctx["publisher"].admin_signals == []


def test_flat_changes_no_settings(ctx):
  _link(ctx)
  resp = ctx["client"].post(
    f"/v1/telegram/{TG_ID}/commands/flat", json={}, headers=_headers()
  )
  # FLAT is an action, not a setting — it must not touch the blob.
  assert resp.json()["settings"] is None
  assert not ctx["account_repo"]._accounts[0].settings


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
  accounts = (
    multi_ctx["client"].get(f"/v1/telegram/{TG_ID}/accounts", headers=_headers()).json()
  )
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


# ── trade-card opt-in follows the active account ───────────────────


class FakeTgBroadcastRepo:
  """In-memory stand-in for ``TradeBroadcastRepository`` mirroring the real
  model: the opt-in is a flag on one ``(platform_user_id, account)`` link, so
  ``flags`` holds exactly those pairs. ``fail`` makes every write report the
  database error the real repository signals with ``False``."""

  def __init__(self, account_repo: "FakeTgAccountRepo | None" = None):
    self.flags: set[tuple[str, uuid.UUID]] = set()
    self.fail = False
    # The bulk switch covers whatever the user is linked to, which only the
    # account repository knows.
    self._account_repo = account_repo

  async def subscribe(self, telegram_user_id, account_uuid, platform=None):
    if self.fail:
      return False
    self.flags.add((str(telegram_user_id), account_uuid))
    return True

  async def unsubscribe(self, telegram_user_id, account_uuid, platform=None):
    if self.fail:
      return False
    self.flags.discard((str(telegram_user_id), account_uuid))
    return True

  async def is_subscribed(self, telegram_user_id, account_uuid, platform=None):
    return (str(telegram_user_id), account_uuid) in self.flags

  async def _set_all(self, telegram_user_id, enabled):
    if self.fail:
      return None
    linked = await self._account_repo.list_by_telegram_user_id(telegram_user_id)
    for account in linked:
      key = (str(telegram_user_id), account.id)
      (self.flags.add if enabled else self.flags.discard)(key)
    return len(linked)

  async def subscribe_all(self, telegram_user_id, platform=None):
    return await self._set_all(telegram_user_id, True)

  async def unsubscribe_all(self, telegram_user_id, platform=None):
    return await self._set_all(telegram_user_id, False)


@pytest.fixture
def broadcast_ctx(multi_ctx):
  """``multi_ctx`` (two accounts) plus the opt-in repository, with TG_ID linked
  to both — ``acc-1`` first, so it is the active one."""
  broadcast_repo = FakeTgBroadcastRepo(multi_ctx["account_repo"])
  overrides = multi_ctx["client"].app.dependency_overrides
  overrides[get_trade_broadcast_repository] = lambda: broadcast_repo
  _link_both(multi_ctx)
  accounts = {a.account_id: a for a in multi_ctx["account_repo"]._accounts}
  return {**multi_ctx, "broadcast_repo": broadcast_repo, "accounts": accounts}


def _switch_to(ctx, account: Account) -> None:
  resp = ctx["client"].post(
    f"/v1/telegram/{TG_ID}/active-account",
    json={"account_id": str(account.id)},
    headers=_headers(),
  )
  assert resp.status_code == 200


def test_subscribe_applies_to_the_active_account_only(broadcast_ctx):
  client = broadcast_ctx["client"]
  acc1 = broadcast_ctx["accounts"]["acc-1"]

  resp = client.post(f"/v1/telegram/{TG_ID}/broadcast/subscribe", headers=_headers())

  assert resp.status_code == 200
  assert resp.json() == {
    "subscribed": True,
    "account_id": "acc-1",
    "market": "FOREX",
    "gateway": None,
  }
  # The other linked account was not touched.
  assert broadcast_ctx["broadcast_repo"].flags == {(str(TG_ID), acc1.id)}
  resp = client.get(f"/v1/telegram/{TG_ID}/broadcast", headers=_headers())
  assert resp.json()["subscribed"] is True
  assert resp.json()["account_id"] == "acc-1"


def test_switching_account_changes_which_account_subscribe_targets(broadcast_ctx):
  client = broadcast_ctx["client"]
  acc1, acc2 = broadcast_ctx["accounts"]["acc-1"], broadcast_ctx["accounts"]["acc-2"]
  client.post(f"/v1/telegram/{TG_ID}/broadcast/subscribe", headers=_headers())

  _switch_to(broadcast_ctx, acc2)

  # acc-1's opt-in must not leak into the freshly selected account.
  resp = client.get(f"/v1/telegram/{TG_ID}/broadcast", headers=_headers())
  assert resp.json() == {
    "subscribed": False,
    "account_id": "acc-2",
    "market": "CRYPTO",
    "gateway": "BINANCE",
  }

  resp = client.post(f"/v1/telegram/{TG_ID}/broadcast/subscribe", headers=_headers())
  assert resp.json()["account_id"] == "acc-2"
  assert broadcast_ctx["broadcast_repo"].flags == {
    (str(TG_ID), acc1.id),
    (str(TG_ID), acc2.id),
  }


def test_unsubscribe_clears_only_the_active_account(broadcast_ctx):
  client = broadcast_ctx["client"]
  acc1, acc2 = broadcast_ctx["accounts"]["acc-1"], broadcast_ctx["accounts"]["acc-2"]
  client.post(f"/v1/telegram/{TG_ID}/broadcast/subscribe", headers=_headers())
  _switch_to(broadcast_ctx, acc2)
  client.post(f"/v1/telegram/{TG_ID}/broadcast/subscribe", headers=_headers())

  resp = client.post(f"/v1/telegram/{TG_ID}/broadcast/unsubscribe", headers=_headers())

  assert resp.status_code == 200
  assert resp.json()["subscribed"] is False
  assert resp.json()["account_id"] == "acc-2"
  # acc-1 is still subscribed: unsubscribing is not "everything I hold".
  assert broadcast_ctx["broadcast_repo"].flags == {(str(TG_ID), acc1.id)}


def test_broadcast_endpoints_need_a_linked_account(multi_ctx):
  """Nothing linked → no active account → nothing to opt in for."""
  client = multi_ctx["client"]
  repo = FakeTgBroadcastRepo(multi_ctx["account_repo"])
  client.app.dependency_overrides[get_trade_broadcast_repository] = lambda: repo

  for method, path in (
    ("GET", f"/v1/telegram/{TG_ID}/broadcast"),
    ("POST", f"/v1/telegram/{TG_ID}/broadcast/subscribe"),
    ("POST", f"/v1/telegram/{TG_ID}/broadcast/unsubscribe"),
    ("POST", f"/v1/telegram/{TG_ID}/broadcast/subscribe-all"),
    ("POST", f"/v1/telegram/{TG_ID}/broadcast/unsubscribe-all"),
  ):
    resp = client.request(method, path, headers=_headers())
    assert resp.status_code == 404, path
  assert repo.flags == set()


def test_broadcast_write_failure_surfaces_as_500(broadcast_ctx):
  broadcast_ctx["broadcast_repo"].fail = True
  client = broadcast_ctx["client"]

  for action in ("subscribe", "unsubscribe", "subscribe-all", "unsubscribe-all"):
    resp = client.post(f"/v1/telegram/{TG_ID}/broadcast/{action}", headers=_headers())
    assert resp.status_code == 500, action


def test_subscribe_all_switches_on_every_linked_account(broadcast_ctx):
  client = broadcast_ctx["client"]
  acc1, acc2 = broadcast_ctx["accounts"]["acc-1"], broadcast_ctx["accounts"]["acc-2"]

  resp = client.post(
    f"/v1/telegram/{TG_ID}/broadcast/subscribe-all", headers=_headers()
  )

  assert resp.status_code == 200
  assert resp.json() == {"subscribed": True, "accounts": 2}
  assert broadcast_ctx["broadcast_repo"].flags == {
    (str(TG_ID), acc1.id),
    (str(TG_ID), acc2.id),
  }
  # Reading is still per account: switching to acc-2 shows it on there too.
  _switch_to(broadcast_ctx, acc2)
  resp = client.get(f"/v1/telegram/{TG_ID}/broadcast", headers=_headers())
  assert resp.json()["account_id"] == "acc-2"
  assert resp.json()["subscribed"] is True


def test_unsubscribe_all_switches_off_every_linked_account(broadcast_ctx):
  client, repo = broadcast_ctx["client"], broadcast_ctx["broadcast_repo"]
  acc1, acc2 = broadcast_ctx["accounts"]["acc-1"], broadcast_ctx["accounts"]["acc-2"]
  # Only acc-2 is on: the bulk switch-off must not care which ones were.
  repo.flags.add((str(TG_ID), acc2.id))

  resp = client.post(
    f"/v1/telegram/{TG_ID}/broadcast/unsubscribe-all", headers=_headers()
  )

  assert resp.status_code == 200
  assert resp.json() == {"subscribed": False, "accounts": 2}
  assert repo.flags == set()
  assert (str(TG_ID), acc1.id) not in repo.flags
  _switch_to(broadcast_ctx, acc2)
  resp = client.get(f"/v1/telegram/{TG_ID}/broadcast", headers=_headers())
  assert resp.json()["subscribed"] is False


def test_bulk_switch_then_per_account_unsubscribe_leaves_the_rest_on(broadcast_ctx):
  """The reason for having both: everything on at once, then trim one account
  with /switch + /unsubscribe."""
  client = broadcast_ctx["client"]
  acc2 = broadcast_ctx["accounts"]["acc-2"]
  client.post(f"/v1/telegram/{TG_ID}/broadcast/subscribe-all", headers=_headers())

  # acc-1 is the active account.
  client.post(f"/v1/telegram/{TG_ID}/broadcast/unsubscribe", headers=_headers())

  assert broadcast_ctx["broadcast_repo"].flags == {(str(TG_ID), acc2.id)}


# ── many users per account ──────────────────────────────────────────

OTHER_TG_ID = 9999


def test_second_user_can_link_same_account(ctx):
  """The account<->bot-user join table's whole point. Under the old scalar
  ``accounts.telegram_user_id`` this silently stole the account from the
  first user; now both hold it."""
  for tg_id in (TG_ID, OTHER_TG_ID):
    resp = ctx["client"].post(
      "/v1/telegram/link",
      json={"token": str(TOKEN), "telegram_user_id": tg_id},
      headers=_headers(),
    )
    assert resp.status_code == 200

  for tg_id in (TG_ID, OTHER_TG_ID):
    resp = ctx["client"].get(f"/v1/telegram/{tg_id}", headers=_headers())
    assert resp.status_code == 200
    assert resp.json()["account_id"] == "acc-1"

  account = ctx["account_repo"]._accounts[0]
  assert ctx["account_repo"].linked_user_ids(account) == [
    str(TG_ID),
    str(OTHER_TG_ID),
  ]


def test_unlink_leaves_other_users_link_intact(ctx):
  for tg_id in (TG_ID, OTHER_TG_ID):
    ctx["client"].post(
      "/v1/telegram/link",
      json={"token": str(TOKEN), "telegram_user_id": tg_id},
      headers=_headers(),
    )

  resp = ctx["client"].post(f"/v1/telegram/{TG_ID}/unlink", headers=_headers())
  assert resp.status_code == 200

  # The unlinking user is gone...
  assert (
    ctx["client"].get(f"/v1/telegram/{TG_ID}", headers=_headers()).status_code == 404
  )
  # ...the other one still has the account.
  resp = ctx["client"].get(f"/v1/telegram/{OTHER_TG_ID}", headers=_headers())
  assert resp.status_code == 200
  assert resp.json()["account_id"] == "acc-1"


def test_relinking_same_account_is_idempotent(ctx):
  for _ in range(2):
    resp = ctx["client"].post(
      "/v1/telegram/link",
      json={"token": str(TOKEN), "telegram_user_id": TG_ID},
      headers=_headers(),
    )
    assert resp.status_code == 200

  resp = ctx["client"].get(f"/v1/telegram/{TG_ID}/accounts", headers=_headers())
  assert len(resp.json()) == 1


# ── max_open_orders ─────────────────────────────────────────────────
#
# The owner's own cap, over the broker-wide default. The endpoint reports the
# *effective* value plus where it came from, so the bot can tell "5 because I
# chose it" from "5 because nobody has" — only the first is theirs to clear.


def _set_max_orders(ctx, value):
  return ctx["client"].post(
    f"/v1/telegram/{TG_ID}/settings/max-open-orders",
    json={"max_open_orders": value},
    headers=_headers(),
  )


def test_get_max_open_orders_falls_back_to_the_global_default(ctx):
  _link(ctx)
  ctx["setting_repo"].values[MAX_OPEN_ORDERS_KEY] = "7"

  resp = ctx["client"].get(
    f"/v1/telegram/{TG_ID}/settings/max-open-orders", headers=_headers()
  )
  assert resp.status_code == 200
  assert resp.json() == {
    "max_open_orders": 7,
    "source": "global",
    "global_max_open_orders": 7,
  }


def test_get_max_open_orders_reports_an_unset_global_as_the_documented_default(ctx):
  _link(ctx)
  resp = ctx["client"].get(
    f"/v1/telegram/{TG_ID}/settings/max-open-orders", headers=_headers()
  )
  assert resp.status_code == 200
  body = resp.json()
  assert body["max_open_orders"] == DEFAULT_MAX_OPEN_ORDERS
  assert body["source"] == "global"


def test_get_max_open_orders_prefers_the_accounts_own_value(ctx):
  _link(ctx)
  ctx["setting_repo"].values[MAX_OPEN_ORDERS_KEY] = "7"
  ctx["account_repo"]._accounts[0].settings = {"max_open_orders": 2}

  resp = ctx["client"].get(
    f"/v1/telegram/{TG_ID}/settings/max-open-orders", headers=_headers()
  )
  assert resp.json() == {
    "max_open_orders": 2,
    "source": "account",
    "global_max_open_orders": 7,
  }


def test_set_max_open_orders_persists_and_pushes(ctx):
  _link(ctx)
  ctx["account_repo"]._accounts[0].gateway = "MT5"

  resp = _set_max_orders(ctx, 3)
  assert resp.status_code == 200
  assert resp.json()["max_open_orders"] == 3
  assert resp.json()["source"] == "account"
  # Persisted: this, not the push, is what the next handshake replays.
  assert ctx["account_repo"]._accounts[0].settings["max_open_orders"] == 3

  # Pushed live, addressed to the worker's composite id.
  pushed = ctx["publisher"].settings_updates[-1]
  assert pushed["account_id"] == "FOREX-MT5-acc-1"
  assert pushed["settings"].max_open_orders == 3


def test_set_max_open_orders_pushes_the_whole_resolved_block(ctx):
  # The worker applies the push through its handshake code path, which reads
  # every setting it knows — so the block carries them all, not just the one
  # that changed.
  _link(ctx)
  ctx["account_repo"]._accounts[0].gateway = "MT5"
  ctx["account_repo"]._accounts[0].settings = {"signal_blocked": True}

  _set_max_orders(ctx, 4)

  pushed = ctx["publisher"].settings_updates[-1]["settings"]
  assert pushed.signal_blocked is True
  assert pushed.max_open_orders == 4


def test_set_max_open_orders_to_zero_is_kept_as_no_limit(ctx):
  _link(ctx)
  ctx["setting_repo"].values[MAX_OPEN_ORDERS_KEY] = "7"
  ctx["account_repo"]._accounts[0].gateway = "MT5"

  resp = _set_max_orders(ctx, 0)
  assert resp.status_code == 200
  # 0 is a choice, not an absence: it must not resolve back to the global 7.
  assert resp.json() == {
    "max_open_orders": 0,
    "source": "account",
    "global_max_open_orders": 7,
  }
  assert ctx["publisher"].settings_updates[-1]["settings"].max_open_orders == 0


def test_set_max_open_orders_to_null_clears_the_override(ctx):
  _link(ctx)
  ctx["setting_repo"].values[MAX_OPEN_ORDERS_KEY] = "7"
  ctx["account_repo"]._accounts[0].gateway = "MT5"
  _set_max_orders(ctx, 2)

  resp = _set_max_orders(ctx, None)
  assert resp.status_code == 200
  assert resp.json() == {
    "max_open_orders": 7,
    "source": "global",
    "global_max_open_orders": 7,
  }
  # The key stays in the blob as null, which reads as "no override".
  assert ctx["account_repo"]._accounts[0].settings["max_open_orders"] is None
  assert ctx["publisher"].settings_updates[-1]["settings"].max_open_orders == 7


def test_set_max_open_orders_rejects_a_negative_value(ctx):
  _link(ctx)
  assert _set_max_orders(ctx, -1).status_code == 422


def test_set_max_open_orders_skips_the_push_without_a_gateway(ctx):
  # No handshake and no trade yet, so there is no worker id to address — the
  # value still persists and reaches the worker on its first handshake.
  _link(ctx)
  assert ctx["account_repo"]._accounts[0].gateway is None

  resp = _set_max_orders(ctx, 3)
  assert resp.status_code == 200
  assert ctx["account_repo"]._accounts[0].settings["max_open_orders"] == 3
  assert ctx["publisher"].settings_updates == []


def test_set_max_open_orders_fails_when_the_write_fails(ctx):
  _link(ctx)

  async def failing_update(account_uuid, patch):
    return None

  ctx["account_repo"].update_settings = failing_update
  assert _set_max_orders(ctx, 3).status_code == 500
  assert ctx["publisher"].settings_updates == []


def test_set_max_open_orders_requires_a_linked_account(ctx):
  assert _set_max_orders(ctx, 3).status_code == 404


# ── auth enforcement ────────────────────────────────────────────────


def test_telegram_requires_api_key():
  app = FastAPI()
  app.include_router(get_core_router())
  app.dependency_overrides[get_account_repository] = lambda: FakeTgAccountRepo(None)
  app.dependency_overrides[get_trade_repository] = lambda: FakeTradeRepo([], 0)
  app.dependency_overrides[get_publisher] = lambda: FakePublisher()
  client = TestClient(app)
  assert client.get(f"/v1/telegram/{TG_ID}").status_code == 401
