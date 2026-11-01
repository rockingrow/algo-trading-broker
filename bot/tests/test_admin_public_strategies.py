"""Tests for the /admin_public_strategies handler.

The allowlist of strategies the public broadcast may carry lives in a broker
setting, so it is edited from here. Covers reading the current value, the
known-strategy hint, submitting a new list, and the explicit "clear it" answer
— which widens the audience back to every strategy rather than turning
anything off.
"""

from __future__ import annotations

from typing import Any, Optional

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage, StorageKey

from app.handlers.admin import (
  cancel_admin_public_strategies,
  cmd_admin_public_strategies,
  receive_admin_public_strategies,
)
from app.states import AdminPublicBroadcastStrategies


class FakeMessage:
  def __init__(self, text: str = ""):
    self.text = text
    self.answers: list[str] = []

  async def answer(self, text: str, reply_markup: Any = None, **kwargs: Any) -> None:
    self.answers.append(text)

  @property
  def last(self) -> str:
    return self.answers[-1]


class FakeAdminBroker:
  def __init__(
    self,
    current: Optional[dict[str, Any]] = None,
    set_result: Optional[dict[str, Any]] = None,
    known: Optional[list[str]] = None,
  ):
    self._current = current
    self._set_result = set_result
    self._known = known
    self.set_calls: list[list[str]] = []

  async def get_public_broadcast_strategies(self) -> Optional[dict[str, Any]]:
    return self._current

  async def set_public_broadcast_strategies(
    self, strategies: list[str]
  ) -> Optional[dict[str, Any]]:
    self.set_calls.append(strategies)
    return self._set_result

  async def admin_list_strategies(self) -> Optional[list[str]]:
    return self._known


def _make_state() -> FSMContext:
  storage = MemoryStorage()
  key = StorageKey(bot_id=1, chat_id=1, user_id=1)
  return FSMContext(storage=storage, key=key)


async def test_cmd_shows_current_and_prompts():
  message = FakeMessage()
  state = _make_state()
  broker = FakeAdminBroker(
    current={"setting": "public_broadcast_strategies", "value": "wt_bb_atr,gold_m5"}
  )

  await cmd_admin_public_strategies(message, state, broker)

  assert "wt_bb_atr,gold_m5" in message.last
  assert "comma-separated" in message.last
  assert (
    await state.get_state()
    == AdminPublicBroadcastStrategies.waiting_for_strategies.state
  )


async def test_cmd_lists_known_strategies_as_a_hint():
  message = FakeMessage()
  broker = FakeAdminBroker(current={"value": ""}, known=["gold_m5", "wt_bb_atr"])

  await cmd_admin_public_strategies(message, _make_state(), broker)

  assert "Known strategies" in message.last
  assert "gold_m5, wt_bb_atr" in message.last


async def test_the_known_strategy_hint_is_capped():
  """The list grows with every strategy ever traded; an over-long prompt would
  fail to send after the FSM state was already set."""
  message = FakeMessage()
  broker = FakeAdminBroker(
    current={"value": ""}, known=[f"strat_{n}" for n in range(45)]
  )

  await cmd_admin_public_strategies(message, _make_state(), broker)

  assert "strat_39" in message.last
  assert "strat_40" not in message.last
  assert "(+5 more)" in message.last


async def test_cmd_shows_every_strategy_public_when_unset():
  message = FakeMessage()
  broker = FakeAdminBroker(current={"value": ""})

  await cmd_admin_public_strategies(message, _make_state(), broker)

  assert "every strategy is public" in message.last
  # No recorded trades yet: the hint is skipped rather than shown empty.
  assert "Known strategies" not in message.last


async def test_cmd_reports_a_failed_read():
  message = FakeMessage()
  state = _make_state()

  await cmd_admin_public_strategies(message, state, FakeAdminBroker(current=None))

  assert "Failed to fetch" in message.last
  assert await state.get_state() is None


async def test_receive_submits_the_split_list():
  message = FakeMessage(" wt_bb_atr , MT5_GOLD_M5_V1 ")
  state = _make_state()
  broker = FakeAdminBroker(
    set_result={
      "setting": "public_broadcast_strategies",
      "value": "wt_bb_atr,MT5_GOLD_M5_V1",
    }
  )

  await receive_admin_public_strategies(message, state, broker)

  assert broker.set_calls == [["wt_bb_atr", "MT5_GOLD_M5_V1"]]
  assert "updated" in message.last
  assert await state.get_state() is None


async def test_a_single_dash_clears_the_allowlist():
  message = FakeMessage("-")
  broker = FakeAdminBroker(set_result={"value": ""})

  await receive_admin_public_strategies(message, _make_state(), broker)

  assert broker.set_calls == [[]]
  assert "every strategy is public" in message.last


async def test_receive_reports_a_failed_write():
  message = FakeMessage("wt_bb_atr")
  state = _make_state()
  broker = FakeAdminBroker(set_result=None)

  await receive_admin_public_strategies(message, state, broker)

  assert "Failed to update" in message.last
  assert await state.get_state() is None


async def test_cancel_clears_the_state():
  message = FakeMessage()
  state = _make_state()
  await state.set_state(AdminPublicBroadcastStrategies.waiting_for_strategies)

  await cancel_admin_public_strategies(message, state)

  assert await state.get_state() is None
  assert "Cancelled" in message.last
