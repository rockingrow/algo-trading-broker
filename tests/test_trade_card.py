"""Tests for the live trade card feature and the admin link-account endpoint.

Covers four layers with in-memory fakes (no DB / NATS / Telegram):
- ``TradeCardService`` — posting, editing, skipping and forgetting cards,
- the card renderer and its keyboard,
- the ``/v1/telegram/*`` per-trade and opt-in endpoints,
- the ``POST /admin/accounts/{uuid}/link-telegram`` endpoint.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from broker.db.models import Account, Trade
from broker.helpers import emoji_constants as em
from broker.helpers.trade_card import (
  CALLBACK_DETAIL,
  CALLBACK_EXIT,
  CALLBACK_SUMMARY,
  format_trade_card,
  is_terminal,
  trade_card_keyboard,
)
from broker.providers import (
  get_account_repository,
  get_publisher,
  get_trade_broadcast_repository,
  get_trade_repository,
)
from broker.router import get_core_router
from broker.schemas.account_schema import AccountLinkSummary, MarketTypeEnum
from broker.schemas.core import SignalActionEnum
from broker.schemas.trade_event_schema import PositionEvent, PositionEventType
from broker.schemas.trade_schema import TradeCard, TradeStatusEnum
from broker.security.ensure_api_key import ensure_api_key
from broker.services.notification_service import EditOutcome
from broker.services.trade_card_service import TradeCardService

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


class FakeCardRepo:
  """In-memory ``trade_notifications``, keyed the way the real table is."""

  def __init__(self, cards: list[TradeCard] | None = None):
    self._cards = {c.id: c for c in (cards or [])}
    self.deleted: list[uuid.UUID] = []

  async def list_for_trade(self, trade_id, platform=None):
    return list(self._cards.values())

  async def record(self, trade_id, chat_id, message_id, status, platform=None):
    card = TradeCard(
      id=uuid.uuid4(), chat_id=chat_id, message_id=message_id, status=status
    )
    self._cards[card.id] = card
    return True

  async def mark_status(self, card_id, status):
    card = self._cards.get(card_id)
    if card is None:
      return False
    self._cards[card_id] = card.model_copy(update={"status": status})
    return True

  async def delete(self, card_id):
    self.deleted.append(card_id)
    self._cards.pop(card_id, None)
    return True


class FakeSettingRepo:
  def __init__(self, values=None):
    self._values = values or {}

  async def get(self, key):
    return self._values.get(key)


class FakeCardNotifier:
  """Stands in for ``TradeCardNotifier``, recording sends and edits."""

  def __init__(self, edit_outcome=EditOutcome.OK, message_id: int | None = 500):
    self.sent: list[tuple[str, str, dict | None]] = []
    self.edits: list[tuple[str, str, str, dict | None]] = []
    self._edit_outcome = edit_outcome
    self._message_id = message_id

  async def send_and_get_message_id(self, target, text, reply_markup=None):
    self.sent.append((target.chat_id, text, reply_markup))
    return None if self._message_id is None else str(self._message_id)

  async def edit_message(self, target, message_id, text, reply_markup=None):
    self.edits.append((target.chat_id, message_id, text, reply_markup))
    return self._edit_outcome


class FakeTradeRepo:
  """Minimal TradeRepository for the per-trade endpoints."""

  def __init__(self, trade: Trade | None, owner_id: int = 555):
    self._trade = trade
    self._owner_id = owner_id
    self.calls: list[tuple] = []

  async def get_for_telegram_user(self, trade_id, telegram_user_id, platform=None):
    self.calls.append((trade_id, telegram_user_id))
    if self._trade is None or trade_id != self._trade.id:
      return None
    if telegram_user_id != self._owner_id:
      return None
    return self._trade


class FakePublisher:
  def __init__(self):
    self.admin_signals: list[dict] = []

  async def publish_admin_signal(self, **kwargs):
    self.admin_signals.append(kwargs)


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


def _make_trade(status=TradeStatusEnum.CLOSED, last_action=None) -> Trade:
  return Trade(
    id=uuid.uuid4(),
    account_id="acc-1",
    market=MarketTypeEnum.FOREX,
    gateway="MT5",
    account_leverage=100,
    account_balance_init=1000.0,
    account_balance=1120.0,
    ref_id="ref-1",
    strategy="BTC-M15",
    strategy_code="LONG|SIG-1",
    symbol="BTCUSDT",
    action=SignalActionEnum.LONG,
    price=65000.0,
    quantity=0.01,
    sl=63000.0,
    tp1=67000.0,
    tp2=69000.0,
    is_running=False,
    risk_percent=1.0,
    status=status,
    last_action=last_action,
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


def _service(*, targets=None, cards=None, notifier=None, card_repo=None):
  return TradeCardService(
    broadcast_repository=FakeBroadcastRepo(targets=targets or []),
    notification_repository=card_repo or FakeCardRepo(cards),
    setting_repository=FakeSettingRepo(),
    notifier=notifier or FakeCardNotifier(),
  )


async def _deliver(svc: TradeCardService, event, trade) -> None:
  """Run one event all the way through the service.

  ``handle_event`` only queues — the TRADE consumer must not wait on Telegram —
  so a test has to start the drain task and let it finish. ``stop`` joins the
  queue before cancelling, which is exactly that wait.
  """
  await svc.start()
  await svc.handle_event(event, trade)
  await svc.stop()


# ── TradeCardService ─────────────────────────────────────────────────


async def test_card_is_posted_to_every_subscriber_when_a_trade_opens():
  notifier = FakeCardNotifier()
  svc = _service(targets=["111", "222"], notifier=notifier)

  await _deliver(svc, _event("OPENED"), _make_trade(TradeStatusEnum.OPENED))

  assert [chat for chat, _, _ in notifier.sent] == ["111", "222"]
  assert notifier.edits == []
  # A running trade's card carries its two buttons.
  for _, _, markup in notifier.sent:
    labels = [b["callback_data"].rsplit(":", 1)[0] for b in markup["inline_keyboard"][0]]
    assert labels == [CALLBACK_DETAIL, CALLBACK_EXIT]


async def test_existing_card_is_edited_not_reposted_on_partial_close():
  card = TradeCard(
    id=uuid.uuid4(), chat_id="111", message_id=42, status=TradeStatusEnum.OPENED
  )
  notifier = FakeCardNotifier()
  svc = _service(targets=["111"], cards=[card], notifier=notifier)

  await _deliver(
    svc,
    _event("TP1"),
    _make_trade(TradeStatusEnum.PARTIALLY_CLOSED, last_action="TP1"),
  )

  assert notifier.sent == []
  assert len(notifier.edits) == 1
  chat_id, message_id, body, markup = notifier.edits[0]
  # The Bot API takes the id as a string; the row stores it as an int.
  assert (chat_id, message_id) == ("111", "42")
  assert "[" + em.CYCLE_RUNNING + "RUNNING]" in body
  assert f"{em.TP1} TP1" in body
  # Still running, so the buttons stay.
  assert markup is not None


async def test_closing_edits_the_card_and_drops_its_buttons():
  card = TradeCard(
    id=uuid.uuid4(),
    chat_id="111",
    message_id=42,
    status=TradeStatusEnum.PARTIALLY_CLOSED,
  )
  notifier = FakeCardNotifier()
  svc = _service(targets=["111"], cards=[card], notifier=notifier)

  await _deliver(
    svc, _event("TP2"), _make_trade(TradeStatusEnum.CLOSED, last_action="TP2")
  )

  assert notifier.sent == []
  _, _, body, markup = notifier.edits[0]
  assert "[" + em.CYCLE_CLOSED + "CLOSED]" in body
  assert f"{em.TP2} TP2" in body
  assert markup is None


async def test_admin_flat_closes_the_card_too():
  card = TradeCard(
    id=uuid.uuid4(), chat_id="111", message_id=42, status=TradeStatusEnum.OPENED
  )
  notifier = FakeCardNotifier()
  svc = _service(targets=["111"], cards=[card], notifier=notifier)

  await _deliver(
    svc, _event("FLATTED"), _make_trade(TradeStatusEnum.FLAT, last_action="FLAT")
  )

  _, _, body, markup = notifier.edits[0]
  assert "[" + em.CYCLE_CLOSED + "CLOSED]" in body
  assert f"{em.FLAT} FLAT" in body
  assert markup is None


async def test_event_that_does_not_change_status_is_skipped():
  """Workers re-emit TRADE events for changes the card doesn't show."""
  card = TradeCard(
    id=uuid.uuid4(), chat_id="111", message_id=42, status=TradeStatusEnum.OPENED
  )
  notifier = FakeCardNotifier()
  svc = _service(targets=["111"], cards=[card], notifier=notifier)

  await _deliver(svc, _event("OPENED"), _make_trade(TradeStatusEnum.OPENED))

  assert notifier.edits == []
  assert notifier.sent == []


async def test_card_is_refreshed_even_after_the_owner_unsubscribed():
  """The opt-in gates *new* cards; an existing one must stay truthful."""
  card = TradeCard(
    id=uuid.uuid4(), chat_id="111", message_id=42, status=TradeStatusEnum.OPENED
  )
  notifier = FakeCardNotifier()
  svc = _service(targets=[], cards=[card], notifier=notifier)

  await _deliver(svc, _event("TP2"), _make_trade(TradeStatusEnum.CLOSED))

  assert len(notifier.edits) == 1
  assert notifier.sent == []


async def test_subscriber_who_joined_mid_trade_still_gets_a_card():
  notifier = FakeCardNotifier()
  svc = _service(targets=["999"], notifier=notifier)

  await _deliver(svc, _event("TP2"), _make_trade(TradeStatusEnum.CLOSED))

  assert [chat for chat, _, _ in notifier.sent] == ["999"]
  # It arrives already final, so without buttons.
  assert notifier.sent[0][2] is None


async def test_unreachable_card_is_forgotten():
  card = TradeCard(
    id=uuid.uuid4(), chat_id="111", message_id=42, status=TradeStatusEnum.OPENED
  )
  repo = FakeCardRepo([card])
  notifier = FakeCardNotifier(edit_outcome=EditOutcome.MISSING)
  svc = _service(targets=["111"], card_repo=repo, notifier=notifier)

  await _deliver(svc, _event("TP2"), _make_trade(TradeStatusEnum.CLOSED))

  assert repo.deleted == [card.id]


async def test_transient_edit_failure_keeps_the_card_for_a_retry():
  card = TradeCard(
    id=uuid.uuid4(), chat_id="111", message_id=42, status=TradeStatusEnum.OPENED
  )
  repo = FakeCardRepo([card])
  notifier = FakeCardNotifier(edit_outcome=EditOutcome.FAILED)
  svc = _service(targets=["111"], card_repo=repo, notifier=notifier)

  await _deliver(svc, _event("TP2"), _make_trade(TradeStatusEnum.CLOSED))

  assert repo.deleted == []
  # Status is unchanged, so the next event tries again.
  assert (await repo.list_for_trade(uuid.uuid4()))[0].status == TradeStatusEnum.OPENED


async def test_failed_send_is_not_recorded_as_a_card():
  repo = FakeCardRepo()
  notifier = FakeCardNotifier(message_id=None)
  svc = _service(targets=["111"], card_repo=repo, notifier=notifier)

  await _deliver(svc, _event("OPENED"), _make_trade(TradeStatusEnum.OPENED))

  assert await repo.list_for_trade(uuid.uuid4()) == []


async def test_no_subscribers_and_no_cards_is_a_no_op():
  notifier = FakeCardNotifier()
  svc = _service(targets=[], notifier=notifier)
  await _deliver(svc, _event("TP2"), _make_trade())
  assert notifier.sent == [] and notifier.edits == []


async def test_none_trade_is_ignored():
  notifier = FakeCardNotifier()
  svc = _service(targets=["111"], notifier=notifier)
  await _deliver(svc, _event("TP2"), None)
  assert notifier.sent == []


async def test_targets_are_scoped_to_the_trades_account():
  repo = FakeBroadcastRepo(targets=["111"])
  svc = TradeCardService(
    broadcast_repository=repo,
    notification_repository=FakeCardRepo(),
    setting_repository=FakeSettingRepo(),
    notifier=FakeCardNotifier(),
  )
  await _deliver(svc, _event("OPENED"), _make_trade(TradeStatusEnum.OPENED))
  assert repo.target_calls == [("acc-1", MarketTypeEnum.FOREX, "MT5")]


async def test_handle_event_does_not_touch_telegram_on_the_trade_path():
  """nats-py awaits this callback before pulling the next TRADE message, so the
  hand-off must return before any Bot API call happens."""
  notifier = FakeCardNotifier()
  svc = _service(targets=["111"], notifier=notifier)

  await svc.handle_event(_event("OPENED"), _make_trade(TradeStatusEnum.OPENED))

  # Queued, not sent — nothing has reached the notifier yet.
  assert notifier.sent == [] and notifier.edits == []
  assert svc.pending == 1

  await svc.start()
  await svc.stop()
  assert [chat for chat, _, _ in notifier.sent] == ["111"]


async def test_a_full_queue_drops_the_update_instead_of_blocking():
  svc = _service(targets=["111"])
  svc._queue = __import__("asyncio").Queue(maxsize=1)

  # Neither call may raise or wait, even though only one fits.
  await svc.handle_event(_event("OPENED"), _make_trade(TradeStatusEnum.OPENED))
  await svc.handle_event(_event("TP1"), _make_trade(TradeStatusEnum.PARTIALLY_CLOSED))
  assert svc.pending == 1


async def test_a_failing_card_does_not_kill_the_drain_task():
  class BoomRepo(FakeCardRepo):
    async def list_for_trade(self, trade_id, platform=None):
      raise RuntimeError("db down")

  notifier = FakeCardNotifier()
  svc = _service(targets=["111"], card_repo=BoomRepo(), notifier=notifier)
  await _deliver(svc, _event("OPENED"), _make_trade(TradeStatusEnum.OPENED))

  # Swallowed, and the worker is still the one that shut down cleanly.
  assert notifier.sent == []


# ── Card rendering ───────────────────────────────────────────────────


def test_card_renders_decimals_plainly():
  """Numeric(20,8) columns come back as Decimals whose str() leaks the scale
  (0 -> '0E-8', 0.104 -> '0.10400000'). The card must show plain numbers."""
  trade = _make_trade()
  trade.price = Decimal("0E-8")
  trade.quantity = Decimal("0.10400000")
  trade.account_balance = Decimal("4328.60279949")

  body = format_trade_card(trade)

  assert "Close price: <code>0</code>" in body
  assert "Quantity: <code>0.104</code>" in body
  assert "Balance: <b>4328.60279949</b>" in body
  assert "E-8" not in body


def test_card_header_matches_the_broadcast_style():
  """Same [STATUS] bracket, divider and entry-icon shape as the public
  broadcast message, so the two read as one visual system."""
  trade = _make_trade(TradeStatusEnum.OPENED, last_action="OPENED")
  lines = format_trade_card(trade).splitlines()
  assert lines[0] == f"[{em.CYCLE_RUNNING}RUNNING]"
  assert lines[1] == f"{em.LONG} <b>LONG</b> <b>BTCUSDT</b>"
  assert lines[2] == "-----------"


def test_card_says_how_a_trade_ended():
  """TP2 / SL / R_SL all persist as CLOSED, so the bracket status alone is not
  enough — the "Actions:" box is what says how a trade ended."""
  trade = _make_trade()
  trade.last_action = "SL"
  body = format_trade_card(trade)
  assert "Actions:" in body
  assert f"{em.SL} SL" in body


def test_fresh_trade_has_no_actions_box():
  """Nothing has happened yet beyond the entry, so there is nothing to box."""
  trade = _make_trade(TradeStatusEnum.OPENED, last_action="OPENED")
  assert "Actions:" not in format_trade_card(trade)


def test_flat_trade_shows_flat_in_the_actions_box():
  trade = _make_trade(TradeStatusEnum.FLAT, last_action="FLAT")
  body = format_trade_card(trade)
  assert "[" + em.CYCLE_CLOSED + "CLOSED]" in body
  assert f"{em.FLAT} FLAT" in body


def test_rejected_trade_shows_rejected_in_the_actions_box():
  """REJECTED collapses to the same [CLOSED] bracket as every other terminal
  status, so the box is the only place this trade never actually opened."""
  trade = _make_trade(TradeStatusEnum.REJECTED, last_action="REJECTED")
  body = format_trade_card(trade)
  assert "[" + em.CYCLE_CLOSED + "CLOSED]" in body
  assert f"{em.TRADE_REJECTED} REJECTED" in body


def test_card_omits_the_actions_box_when_the_row_predates_the_column():
  trade = _make_trade()
  trade.last_action = None
  assert "Actions:" not in format_trade_card(trade)


def test_summary_card_has_pnl_and_hides_the_detail_block():
  body = format_trade_card(_make_trade())
  assert "BTCUSDT" in body
  assert "+120.00" in body  # 1120 - 1000
  assert "[" + em.CYCLE_CLOSED + "CLOSED]" in body
  assert "Strategy" not in body
  assert "Account:" not in body


def test_detailed_card_adds_the_bookkeeping_block():
  body = format_trade_card(_make_trade(TradeStatusEnum.OPENED), detailed=True)
  assert "Strategy: <b>BTC-M15</b>" in body
  assert "Account: <code>acc-1</code>" in body
  assert "Market: <b>FOREX</b> / <b>MT5</b>" in body
  assert "Leverage: <b>100</b>" in body
  assert "Ref: <code>ref-1</code>" in body
  assert "Opened:" in body
  # An open trade shows the live price, not a close price.
  assert "Price: <code>65000</code>" in body
  # Risk sits in the always-visible entry block now, not the detail-only one.
  assert "Risk: <code>1%</code>" in body


def test_card_escapes_worker_supplied_text():
  trade = _make_trade(TradeStatusEnum.REJECTED)
  trade.reject_reason = "MAX ORDER <limit> hit"
  body = format_trade_card(trade, detailed=True)
  assert "&lt;limit&gt;" in body
  assert "<limit>" not in body


def test_card_footer_is_appended_when_given():
  body = format_trade_card(_make_trade(TradeStatusEnum.OPENED), footer="waiting…")
  assert body.endswith("waiting…")


def test_keyboard_toggles_between_detail_and_summary():
  trade = _make_trade(TradeStatusEnum.OPENED)
  summary_view = trade_card_keyboard(trade)
  detail_view = trade_card_keyboard(trade, detailed=True)

  assert summary_view["inline_keyboard"][0][0]["callback_data"] == (
    f"{CALLBACK_DETAIL}:{trade.id}"
  )
  assert detail_view["inline_keyboard"][0][0]["callback_data"] == (
    f"{CALLBACK_SUMMARY}:{trade.id}"
  )


def test_keyboard_callback_data_fits_telegrams_limit():
  trade = _make_trade(TradeStatusEnum.OPENED)
  for row in trade_card_keyboard(trade)["inline_keyboard"]:
    for button in row:
      assert len(button["callback_data"].encode()) <= 64


@pytest.mark.parametrize(
  "status",
  [TradeStatusEnum.CLOSED, TradeStatusEnum.FLAT, TradeStatusEnum.REJECTED],
)
def test_terminal_trades_have_no_keyboard(status):
  trade = _make_trade(status)
  assert is_terminal(trade) is True
  assert trade_card_keyboard(trade) is None


@pytest.mark.parametrize(
  "status", [TradeStatusEnum.OPENED, TradeStatusEnum.PARTIALLY_CLOSED]
)
def test_running_trades_keep_their_keyboard(status):
  trade = _make_trade(status)
  assert is_terminal(trade) is False
  assert trade_card_keyboard(trade) is not None


# ── Per-trade endpoints ──────────────────────────────────────────────


@pytest.fixture
def trade_ctx():
  trade = _make_trade(TradeStatusEnum.OPENED)
  repo = FakeTradeRepo(trade)
  publisher = FakePublisher()
  app = FastAPI()
  app.include_router(get_core_router())
  app.dependency_overrides[get_trade_repository] = lambda: repo
  app.dependency_overrides[get_publisher] = lambda: publisher
  app.dependency_overrides[ensure_api_key] = lambda: None
  return {
    "client": TestClient(app),
    "trade": trade,
    "repo": repo,
    "publisher": publisher,
    "headers": {"X-API-KEY": API_KEY},
  }


def test_get_trade_returns_the_owners_trade(trade_ctx):
  trade = trade_ctx["trade"]
  r = trade_ctx["client"].get(
    f"/v1/telegram/555/trades/{trade.id}", headers=trade_ctx["headers"]
  )
  assert r.status_code == 200
  assert r.json()["symbol"] == "BTCUSDT"
  assert r.json()["status"] == "OPENED"
  # Regression: these dropped out of the response entirely, so the card's
  # Detail view showed "Market: — / —" no matter what the row held.
  assert r.json()["market"] == "FOREX"
  assert r.json()["gateway"] == "MT5"


def test_get_trade_404s_for_another_users_trade(trade_ctx):
  trade = trade_ctx["trade"]
  r = trade_ctx["client"].get(
    f"/v1/telegram/777/trades/{trade.id}", headers=trade_ctx["headers"]
  )
  assert r.status_code == 404


def test_exit_publishes_a_flat_scoped_to_the_trade(trade_ctx):
  trade = trade_ctx["trade"]
  r = trade_ctx["client"].post(
    f"/v1/telegram/555/trades/{trade.id}/exit", headers=trade_ctx["headers"]
  )
  assert r.status_code == 200
  assert r.json()["action"] == "FLAT"

  published = trade_ctx["publisher"].admin_signals
  assert len(published) == 1
  assert published[0]["strategy"] == "BTC-M15"
  assert published[0]["symbol"] == "BTCUSDT"
  assert published[0]["account_id"] == "acc-1"
  assert published[0]["gateway"] == "MT5"
  # The trade's own ref_id rides along so a worker can scope the FLAT to this
  # exact position instead of matching every open position on the scope.
  assert published[0]["ref_id"] == "ref-1"


def test_exit_404s_for_another_users_trade(trade_ctx):
  trade = trade_ctx["trade"]
  r = trade_ctx["client"].post(
    f"/v1/telegram/777/trades/{trade.id}/exit", headers=trade_ctx["headers"]
  )
  assert r.status_code == 404
  assert trade_ctx["publisher"].admin_signals == []


def test_exit_409s_on_an_already_closed_trade():
  trade = _make_trade(TradeStatusEnum.CLOSED)
  publisher = FakePublisher()
  app = FastAPI()
  app.include_router(get_core_router())
  app.dependency_overrides[get_trade_repository] = lambda: FakeTradeRepo(trade)
  app.dependency_overrides[get_publisher] = lambda: publisher
  app.dependency_overrides[ensure_api_key] = lambda: None

  r = TestClient(app).post(
    f"/v1/telegram/555/trades/{trade.id}/exit", headers={"X-API-KEY": API_KEY}
  )
  assert r.status_code == 409
  assert publisher.admin_signals == []


# ── Trade-card opt-in endpoints ──────────────────────────────────────


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
