"""Tests for /admin_flat — the bare scope-picker flow (strategy → market →
gateway → confirm, each with an "All" row) and the single-account confirm
path. The picker handlers steer FSM state; the confirm callback calls the
broker with whichever scope keys survived. The single-account branch is
covered here too so a change to one flow doesn't quietly break the other."""

from __future__ import annotations

from typing import Any

from aiogram.filters.command import CommandObject

from app.handlers.admin import (
  cb_aflat,
  cb_aflat_pick_gateway,
  cb_aflat_pick_market,
  cb_aflat_pick_strategy,
  cmd_aflat,
)
from app.keyboards import inline


ACCOUNT_A = {
  "id": "11111111-1111-1111-1111-111111111111",
  "account_id": "acc-1",
  "market": "CRYPTO",
  "gateway": "BINANCE",
  "account_name": "Main",
}


class FakeMessage:
  def __init__(self):
    self.answers: list[str] = []
    self.keyboards: list[Any] = []

  async def answer(self, text, reply_markup=None, **_):
    self.answers.append(text)
    self.keyboards.append(reply_markup)

  async def edit_text(self, text, reply_markup=None, **_):
    self.answers.append(text)
    self.keyboards.append(reply_markup)

  @property
  def last(self):
    return self.answers[-1]


class FakeCall:
  def __init__(self, data):
    self.data = data
    self.message = FakeMessage()
    self.alerts: list[str] = []

  async def answer(self, text=None, show_alert=False):
    self.alerts.append(text)


class FakeState:
  """Minimal in-memory FSMContext replacement."""

  def __init__(self, initial=None):
    self._data: dict[str, Any] = dict(initial or {})

  async def get_data(self):
    return dict(self._data)

  async def update_data(self, **kwargs):
    for k, v in kwargs.items():
      self._data[k] = v

  async def clear(self):
    self._data.clear()


class FakeAdminBroker:
  def __init__(self, accounts=None, strategies=None):
    self.accounts = accounts or []
    self.strategies = strategies or []
    self.flat_calls: list[dict[str, Any]] = []

  async def admin_list_accounts(self):
    return self.accounts

  async def admin_list_strategies(self):
    return list(self.strategies)

  async def admin_flat(self, **kwargs):
    self.flat_calls.append(kwargs)
    return {"action": "FLAT", "scope": "ALL"}


# ── /aflat with no arg opens the scope pickers ─────────────────────


async def test_cmd_aflat_bare_opens_strategy_picker_with_all_row():
  msg = FakeMessage()
  state = FakeState()
  broker = FakeAdminBroker(strategies=["strat_a", "strat_b"])

  await cmd_aflat(msg, CommandObject(command="admin_flat", args=None), state, broker)

  buttons = msg.keyboards[-1].inline_keyboard
  # First row must be the All sentinel — every step keeps a broad option.
  assert buttons[0][0].callback_data == f"afls:{inline.AFLAT_ALL}"
  labels = [row[0].text for row in buttons]
  assert labels[0].startswith("All")
  assert "strat_a" in labels
  assert "strat_b" in labels
  # FSM primed for the pick-strategy handler.
  data = await state.get_data()
  assert data["aflat_strategies"] == ["strat_a", "strat_b"]
  assert data["aflat_target"] == "*"


async def test_all_strategy_market_gateway_flat_calls_broker_without_scope():
  broker = FakeAdminBroker(strategies=["strat_a"])
  state = FakeState()
  msg = FakeMessage()
  await cmd_aflat(msg, CommandObject(command="admin_flat", args=None), state, broker)

  call = FakeCall(f"afls:{inline.AFLAT_ALL}")
  await cb_aflat_pick_strategy(call, state)
  call = FakeCall(f"aflm:{inline.AFLAT_ALL}")
  await cb_aflat_pick_market(call, state)
  call = FakeCall(f"aflg:{inline.AFLAT_ALL}")
  await cb_aflat_pick_gateway(call, state)

  call = FakeCall("aflat:confirm")
  await cb_aflat(call, state, broker)

  assert broker.flat_calls == [
    {"strategy": None, "market": None, "gateway": None}
  ]


async def test_scoped_pickers_forward_selected_values_to_broker():
  broker = FakeAdminBroker(strategies=["strat_a", "strat_b"])
  state = FakeState()
  msg = FakeMessage()
  await cmd_aflat(msg, CommandObject(command="admin_flat", args=None), state, broker)

  # Strategy: index 1 = "strat_b".
  await cb_aflat_pick_strategy(FakeCall("afls:1"), state)
  # Market: CRYPTO.
  await cb_aflat_pick_market(FakeCall("aflm:CRYPTO"), state)
  # Gateway: BINANCE (must be valid for the CRYPTO market).
  await cb_aflat_pick_gateway(FakeCall("aflg:BINANCE"), state)

  await cb_aflat(FakeCall("aflat:confirm"), state, broker)
  assert broker.flat_calls == [
    {"strategy": "strat_b", "market": "CRYPTO", "gateway": "BINANCE"}
  ]


async def test_gateway_picker_rejects_gateway_not_valid_for_chosen_market():
  state = FakeState(
    initial={
      "aflat_target": "*",
      "aflat_strategies": [],
      "aflat_scope": {"strategy": None, "market": "CRYPTO", "gateway": None},
    }
  )
  # MT5 is not a CRYPTO gateway — the callback must be ignored (no scope change).
  call = FakeCall("aflg:MT5")
  await cb_aflat_pick_gateway(call, state)
  data = await state.get_data()
  assert data["aflat_scope"]["gateway"] is None
  assert call.message.answers == []  # no confirm prompt emitted


async def test_all_markets_lets_any_configured_gateway_through():
  state = FakeState(
    initial={
      "aflat_target": "*",
      "aflat_strategies": [],
      "aflat_scope": {"strategy": None, "market": None, "gateway": None},
    }
  )
  # With market=All the gateway picker offers the union of every configured
  # gateway, so MT5 (from FOREX) is legal even though market is unset.
  await cb_aflat_pick_gateway(FakeCall("aflg:MT5"), state)
  data = await state.get_data()
  assert data["aflat_scope"]["gateway"] == "MT5"


async def test_expired_strategy_index_alerts_and_leaves_scope_unchanged():
  state = FakeState(
    initial={
      "aflat_target": "*",
      "aflat_strategies": ["strat_a"],
      "aflat_scope": {"strategy": None, "market": None, "gateway": None},
    }
  )
  call = FakeCall("afls:99")
  await cb_aflat_pick_strategy(call, state)
  assert call.alerts and "Expired" in (call.alerts[-1] or "")
  data = await state.get_data()
  assert data["aflat_scope"]["strategy"] is None


# ── /aflat <account_id> keeps the single-account confirm path ──────


async def test_cmd_aflat_single_account_arg_confirms_that_account():
  msg = FakeMessage()
  state = FakeState()
  broker = FakeAdminBroker(accounts=[ACCOUNT_A])

  await cmd_aflat(msg, CommandObject(command="admin_flat", args="acc-1"), state, broker)
  assert "acc-1" in msg.last
  data = await state.get_data()
  assert data["aflat_target"]["account_id"] == "acc-1"


async def test_confirm_forwards_account_market_gateway_when_target_is_scoped():
  state = FakeState(
    initial={
      "aflat_target": ACCOUNT_A,
      "aflat_scope": None,
    }
  )
  broker = FakeAdminBroker()
  await cb_aflat(FakeCall("aflat:confirm"), state, broker)
  assert broker.flat_calls == [
    {"account_id": "acc-1", "market": "CRYPTO", "gateway": "BINANCE"}
  ]


async def test_cancel_never_calls_the_broker():
  state = FakeState(initial={"aflat_target": "*", "aflat_scope": None})
  broker = FakeAdminBroker()
  await cb_aflat(FakeCall("aflat:cancel"), state, broker)
  assert broker.flat_calls == []
