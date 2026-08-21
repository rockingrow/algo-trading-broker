"""Tests for the live trade card's bot side: rendering, keyboards, the two
new broker-client calls, and the callback handlers behind the buttons.

The card is posted by the broker; this covers what happens once a user taps
something on it.
"""

from __future__ import annotations

import httpx
import pytest
from aiogram.types import InlineKeyboardMarkup

from app import emojis
from app.constants import (
  CB_TRADE_DETAIL,
  CB_TRADE_EXIT,
  CB_TRADE_EXIT_CANCEL,
  CB_TRADE_EXIT_CONFIRM,
  CB_TRADE_SUMMARY,
)
from app.handlers import trade_card as handlers
from app.keyboards import inline
from app.presenters import trade_card as card
from app.services.broker_client import BrokerClientUser

TRADE_ID = "3fa85f64-5717-4562-b3fc-2c963f66afa6"


def _trade(**overrides):
  base = {
    "id": TRADE_ID,
    "account_id": "acc-1",
    "account_leverage": 100,
    "account_balance_init": 1000.0,
    "account_balance": 1120.0,
    "ref_id": "ref-1",
    "comment": None,
    "strategy_code": "LONG|SIG-1",
    "gateway_return_code": 0,
    "strategy": "BTC-M15",
    "market": "CRYPTO",
    "gateway": "BINANCE",
    "symbol": "BTCUSDT",
    "action": "LONG",
    "price": 65000.0,
    "quantity": 0.01,
    "sl": 63000.0,
    "tp1": 67000.0,
    "tp2": 69000.0,
    "is_running": True,
    "risk_percent": 1.0,
    "status": "OPENED",
    "last_action": "OPENED",
    "reject_reason": None,
    "createdAt": "2026-01-01T00:00:00Z",
    "updatedAt": "2026-01-02T03:04:00Z",
  }
  base.update(overrides)
  return base


# ── Rendering ────────────────────────────────────────────────────────


def test_summary_card_shows_the_essentials():
  body = card.format_trade_card(_trade(), 7.0)
  assert "<b>BTCUSDT</b>" in body
  assert f"[{emojis.CYCLE_RUNNING}RUNNING]" in body
  assert "Price: <code>65000</code>" in body
  assert "Quantity: <code>0.01</code>" in body
  assert "SL: <code>63000</code>" in body
  assert "PnL: <b>+120.00</b>" in body
  assert "Updated: 2026-01-02 10:04:00 (UTC+7)" in body
  # The bookkeeping block belongs to the detail view only.
  assert "Strategy:" not in body


def test_detailed_card_adds_the_bookkeeping_block():
  body = card.format_trade_card(_trade(), 7.0, detailed=True)
  assert "Strategy: <b>BTC-M15</b>" in body
  assert "Account: <code>acc-1</code>" in body
  assert "Market: <b>CRYPTO</b> / <b>BINANCE</b>" in body
  assert "Leverage: <b>100</b>" in body
  assert "Risk: <code>1%</code>" in body
  assert "Opened: 2026-01-01 07:00:00 (UTC+7)" in body


def test_card_says_how_a_trade_ended():
  """Must match the broker's twin, or a Detail tap would drop the SL entry."""
  body = card.format_trade_card(_trade(status="CLOSED", last_action="SL"), 7.0)
  assert "Actions:" in body
  assert f"{emojis.SL} SL" in body


def test_fresh_trade_has_no_actions_box():
  """Nothing has happened yet beyond the entry, so there is nothing to box."""
  body = card.format_trade_card(_trade(), 7.0)
  assert "Actions:" not in body


def test_flat_trade_shows_flat_in_the_actions_box():
  body = card.format_trade_card(_trade(status="FLAT", last_action="FLAT"), 7.0)
  assert f"[{emojis.CYCLE_CLOSED}CLOSED]" in body
  assert f"{emojis.FLAT} FLAT" in body


def test_closed_card_labels_the_price_as_a_close():
  body = card.format_trade_card(_trade(status="CLOSED"), 7.0)
  assert "Close price: <code>65000</code>" in body
  assert f"[{emojis.CYCLE_CLOSED}CLOSED]" in body


def test_numbers_render_without_the_json_float_tail():
  """The broker normalises Decimals; over JSON the same values are floats."""
  assert card._num(65000.0) == "65000"
  assert card._num(0.104) == "0.104"
  assert card._num(0.0) == "0"
  assert card._num(None) == "—"


def test_worker_text_is_escaped():
  body = card.format_trade_card(
    _trade(status="REJECTED", reject_reason="MAX ORDER <limit> hit"), 7.0, detailed=True
  )
  assert "&lt;limit&gt;" in body
  assert "<limit>" not in body


def test_footer_is_appended_when_given():
  body = card.format_trade_card(_trade(), 7.0, footer="waiting…")
  assert body.endswith("waiting…")


@pytest.mark.parametrize("status", ["CLOSED", "FLAT", "REJECTED"])
def test_terminal_statuses_are_closed(status):
  assert card.is_closed(_trade(status=status)) is True


@pytest.mark.parametrize("status", ["OPENED", "PARTIALLY_CLOSED"])
def test_running_statuses_are_not_closed(status):
  assert card.is_closed(_trade(status=status)) is False


# ── Keyboards ────────────────────────────────────────────────────────


def test_card_keyboard_toggles_between_views():
  summary = inline.trade_card(TRADE_ID, detailed=False, closed=False)
  detail = inline.trade_card(TRADE_ID, detailed=True, closed=False)
  assert summary.inline_keyboard[0][0].callback_data == f"{CB_TRADE_DETAIL}:{TRADE_ID}"
  assert summary.inline_keyboard[0][1].callback_data == f"{CB_TRADE_EXIT}:{TRADE_ID}"
  assert detail.inline_keyboard[0][0].callback_data == f"{CB_TRADE_SUMMARY}:{TRADE_ID}"
  # Same glyph as the [CLOSED] header, so the button reads as "ends the trade".
  assert summary.inline_keyboard[0][1].text == f"{emojis.CYCLE_CLOSED} Close"


def test_closed_card_has_no_keyboard():
  assert inline.trade_card(TRADE_ID, detailed=False, closed=True) is None


def test_exit_confirm_keyboard_offers_both_ways_out():
  kb = inline.trade_exit_confirm(TRADE_ID)
  data = [b.callback_data for b in kb.inline_keyboard[0]]
  assert data == [
    f"{CB_TRADE_EXIT_CONFIRM}:{TRADE_ID}",
    f"{CB_TRADE_EXIT_CANCEL}:{TRADE_ID}",
  ]


def test_all_callback_data_fits_telegrams_limit():
  keyboards = [
    inline.trade_card(TRADE_ID, detailed=False, closed=False),
    inline.trade_card(TRADE_ID, detailed=True, closed=False),
    inline.trade_exit_confirm(TRADE_ID),
  ]
  for kb in keyboards:
    for row in kb.inline_keyboard:
      for button in row:
        assert len(button.callback_data.encode()) <= 64


# ── Broker client ────────────────────────────────────────────────────


def _client(handler):
  return BrokerClientUser(
    base_url="http://broker:8080",
    api_key="secret-key",
    transport=httpx.MockTransport(handler),
  )


async def test_get_trade_hits_the_per_trade_endpoint():
  captured = {}

  def handler(request: httpx.Request) -> httpx.Response:
    captured["path"] = request.url.path
    return httpx.Response(200, json=_trade())

  client = _client(handler)
  result = await client.get_trade(7, TRADE_ID)
  assert result["symbol"] == "BTCUSDT"
  assert captured["path"] == f"/v1/telegram/7/trades/{TRADE_ID}"
  await client.aclose()


async def test_get_trade_returns_none_when_not_yours():
  client = _client(lambda request: httpx.Response(404))
  assert await client.get_trade(7, TRADE_ID) is None
  await client.aclose()


async def test_exit_trade_posts_to_the_exit_endpoint():
  captured = {}

  def handler(request: httpx.Request) -> httpx.Response:
    captured["path"] = request.url.path
    captured["method"] = request.method
    return httpx.Response(200, json={"action": "FLAT", "scope": "x"})

  client = _client(handler)
  result = await client.exit_trade(7, TRADE_ID)
  assert result["action"] == "FLAT"
  assert captured["path"] == f"/v1/telegram/7/trades/{TRADE_ID}/exit"
  assert captured["method"] == "POST"
  await client.aclose()


async def test_exit_trade_returns_none_on_conflict():
  """The broker answers 409 when the trade closed first."""
  client = _client(lambda request: httpx.Response(409, json={"detail": "closed"}))
  assert await client.exit_trade(7, TRADE_ID) is None
  await client.aclose()


# ── Callback handlers ────────────────────────────────────────────────


class FakeMessage:
  def __init__(self):
    self.text: str | None = None
    self.markup: InlineKeyboardMarkup | None = None
    self.edits = 0

  async def edit_text(self, text, reply_markup=None):
    self.text = text
    self.markup = reply_markup
    self.edits += 1


class FakeUser:
  id = 7


class FakeCall:
  def __init__(self, data: str):
    self.data = data
    self.message = FakeMessage()
    self.from_user = FakeUser()
    self.answers: list[tuple] = []

  async def answer(self, text=None, show_alert=False):
    self.answers.append((text, show_alert))


class FakeBroker:
  def __init__(self, trade=None, exit_result=None):
    self._trade = trade
    self._exit_result = exit_result
    self.exits: list[tuple] = []

  async def get_trade(self, telegram_user_id, trade_id):
    return self._trade

  async def exit_trade(self, telegram_user_id, trade_id):
    self.exits.append((telegram_user_id, trade_id))
    return self._exit_result


class FakeBrokerAdmin:
  async def get_notification_timezone(self):
    return {"setting": "notification_timezone", "value": "7"}


async def test_detail_expands_the_card_in_place():
  call = FakeCall(f"{CB_TRADE_DETAIL}:{TRADE_ID}")
  await handlers.cb_detail(call, FakeBroker(_trade()), FakeBrokerAdmin())

  assert "Strategy: <b>BTC-M15</b>" in call.message.text
  # The toggle flips to Summary while the Exit button stays.
  assert call.message.markup.inline_keyboard[0][0].callback_data == (
    f"{CB_TRADE_SUMMARY}:{TRADE_ID}"
  )
  assert call.answers == [(None, False)]


async def test_summary_collapses_the_card_again():
  call = FakeCall(f"{CB_TRADE_SUMMARY}:{TRADE_ID}")
  await handlers.cb_summary(call, FakeBroker(_trade()), FakeBrokerAdmin())

  assert "Strategy:" not in call.message.text
  assert call.message.markup.inline_keyboard[0][0].callback_data == (
    f"{CB_TRADE_DETAIL}:{TRADE_ID}"
  )


async def test_a_closed_trade_renders_without_buttons():
  call = FakeCall(f"{CB_TRADE_SUMMARY}:{TRADE_ID}")
  await handlers.cb_summary(call, FakeBroker(_trade(status="CLOSED")), FakeBrokerAdmin())

  assert f"[{emojis.CYCLE_CLOSED}CLOSED]" in call.message.text
  assert call.message.markup is None


async def test_a_trade_that_isnt_yours_only_gets_an_alert():
  call = FakeCall(f"{CB_TRADE_DETAIL}:{TRADE_ID}")
  await handlers.cb_detail(call, FakeBroker(None), FakeBrokerAdmin())

  assert call.message.edits == 0
  assert call.answers == [("This trade is no longer available.", True)]


async def test_exit_replaces_the_card_with_a_confirmation():
  call = FakeCall(f"{CB_TRADE_EXIT}:{TRADE_ID}")
  await handlers.cb_exit(call, FakeBroker(_trade()))

  assert "Close <b>BTCUSDT</b>" in call.message.text
  data = [b.callback_data for b in call.message.markup.inline_keyboard[0]]
  assert data == [
    f"{CB_TRADE_EXIT_CONFIRM}:{TRADE_ID}",
    f"{CB_TRADE_EXIT_CANCEL}:{TRADE_ID}",
  ]


async def test_exit_refuses_an_already_closed_trade():
  broker = FakeBroker(_trade(status="CLOSED"))
  call = FakeCall(f"{CB_TRADE_EXIT}:{TRADE_ID}")
  await handlers.cb_exit(call, broker)

  assert call.message.edits == 0
  assert call.answers == [("This trade is already closed.", True)]


async def test_cancel_puts_the_card_back():
  call = FakeCall(f"{CB_TRADE_EXIT_CANCEL}:{TRADE_ID}")
  await handlers.cb_exit_cancel(call, FakeBroker(_trade()), FakeBrokerAdmin())

  assert f"[{emojis.CYCLE_RUNNING}RUNNING]" in call.message.text
  assert call.message.markup is not None


async def test_confirm_publishes_the_exit_and_notes_it_on_the_card():
  broker = FakeBroker(_trade(), exit_result={"action": "FLAT", "scope": "x"})
  call = FakeCall(f"{CB_TRADE_EXIT_CONFIRM}:{TRADE_ID}")
  await handlers.cb_exit_confirm(call, broker, FakeBrokerAdmin())

  assert broker.exits == [(7, TRADE_ID)]
  assert "Close requested" in call.message.text
  # Still open, so the buttons stay until the worker reports the close.
  assert call.message.markup is not None
  assert call.answers == [(None, False)]


async def test_a_failed_exit_alerts_and_refreshes_without_the_footer():
  broker = FakeBroker(_trade(), exit_result=None)
  call = FakeCall(f"{CB_TRADE_EXIT_CONFIRM}:{TRADE_ID}")
  await handlers.cb_exit_confirm(call, broker, FakeBrokerAdmin())

  assert "Close requested" not in call.message.text
  assert call.answers[0][1] is True


async def test_malformed_callback_data_is_ignored():
  call = FakeCall(CB_TRADE_DETAIL)
  await handlers.cb_detail(call, FakeBroker(_trade()), FakeBrokerAdmin())
  assert call.message.edits == 0
  assert call.answers == [(None, False)]


async def test_unknown_card_button_tells_the_user_plainly():
  call = FakeCall("tc:zz:whatever")
  await handlers.cb_unknown(call)
  assert call.answers[0][1] is True
  assert "isn't supported" in call.answers[0][0]
