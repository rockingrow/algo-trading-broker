"""Tests for the /maxorders handler.

Cover the read-current + prompt path, the three ways a value can be spelled
(a number, 0 for no limit, "-" to inherit), the validation the bot does locally
so the broker doesn't have to bounce an obvious typo back, and the account
pinning — the reply to this prompt is a bare number, so the handler has to be
sure it is still writing to the account the prompt was about.
"""

from __future__ import annotations

from typing import Any, Optional

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage, StorageKey

from app.handlers.commands import (
  cancel_max_orders,
  cmd_max_orders,
  receive_max_orders,
)
from app.states import SetMaxOpenOrders


class FakeMessage:
  def __init__(self, text: str = ""):
    self.text = text
    self.answers: list[str] = []
    self.from_user = type("U", (), {"id": 777})()

  async def answer(self, text: str, reply_markup: Any = None, **kwargs: Any) -> None:
    self.answers.append(text)

  @property
  def last(self) -> str:
    return self.answers[-1]


class FakeUserBroker:
  def __init__(
    self,
    current: Optional[dict[str, Any]] = None,
    set_result: Optional[dict[str, Any]] = None,
  ):
    self._current = current
    self._set_result = set_result
    self.set_calls: list[Optional[int]] = []

  async def get_max_open_orders(self, telegram_user_id: int):
    return self._current

  async def set_max_open_orders(
    self, telegram_user_id: int, max_open_orders: Optional[int]
  ):
    self.set_calls.append(max_open_orders)
    return self._set_result


def _make_state() -> FSMContext:
  storage = MemoryStorage()
  key = StorageKey(bot_id=1, chat_id=1, user_id=1)
  return FSMContext(storage=storage, key=key)


def _account(account_id: str = "acc-1") -> dict[str, Any]:
  return {"account_id": account_id}


def _state_payload(limit: int, source: str = "account", global_limit: int = 5):
  return {
    "max_open_orders": limit,
    "source": source,
    "global_max_open_orders": global_limit,
  }


# ── prompt ──────────────────────────────────────────────────────────


async def test_cmd_shows_the_current_cap_and_prompts():
  message = FakeMessage()
  state = _make_state()
  broker = FakeUserBroker(current=_state_payload(3))

  await cmd_max_orders(message, state, _account(), broker)

  assert "3" in message.last
  assert await state.get_state() == SetMaxOpenOrders.waiting_for_value.state


async def test_cmd_names_the_account_the_prompt_is_about():
  # The reply is a bare number, so the user has to be able to see which
  # account they are about to change.
  message = FakeMessage()
  state = _make_state()
  broker = FakeUserBroker(current=_state_payload(3))

  await cmd_max_orders(message, state, _account("acc-42"), broker)

  assert "acc-42" in message.last


async def test_cmd_pins_the_account_in_the_fsm_data():
  message = FakeMessage()
  state = _make_state()
  broker = FakeUserBroker(current=_state_payload(3))

  await cmd_max_orders(message, state, _account("acc-42"), broker)

  assert (await state.get_data())["account_id"] == "acc-42"


async def test_cmd_says_inherited_when_the_cap_is_the_global_default():
  message = FakeMessage()
  state = _make_state()
  broker = FakeUserBroker(current=_state_payload(5, source="global"))

  await cmd_max_orders(message, state, _account(), broker)

  assert "broker-wide default" in message.last


async def test_cmd_renders_zero_as_no_limit():
  message = FakeMessage()
  state = _make_state()
  broker = FakeUserBroker(current=_state_payload(0))

  await cmd_max_orders(message, state, _account(), broker)

  assert "no limit" in message.last


async def test_cmd_broker_failure_sets_no_state():
  message = FakeMessage()
  state = _make_state()
  broker = FakeUserBroker(current=None)

  await cmd_max_orders(message, state, _account(), broker)

  assert "Failed to fetch" in message.last
  assert await state.get_state() is None


# ── submit ──────────────────────────────────────────────────────────


async def _prompted(account_id: str = "acc-1") -> FSMContext:
  state = _make_state()
  await state.set_state(SetMaxOpenOrders.waiting_for_value)
  await state.update_data(account_id=account_id)
  return state


async def test_receive_forwards_the_number_and_clears_state():
  message = FakeMessage(text="3")
  state = await _prompted()
  broker = FakeUserBroker(set_result=_state_payload(3))

  await receive_max_orders(message, state, _account(), broker)

  assert broker.set_calls == [3]
  assert await state.get_state() is None


async def test_receive_forwards_zero_rather_than_treating_it_as_empty():
  message = FakeMessage(text="0")
  state = await _prompted()
  broker = FakeUserBroker(set_result=_state_payload(0))

  await receive_max_orders(message, state, _account(), broker)

  assert broker.set_calls == [0]


async def test_receive_dash_clears_the_override_as_null():
  # "-" and 0 are different intents: inherit the default vs. no cap at all.
  message = FakeMessage(text="-")
  state = await _prompted()
  broker = FakeUserBroker(set_result=_state_payload(5, source="global"))

  await receive_max_orders(message, state, _account(), broker)

  assert broker.set_calls == [None]


async def test_receive_rejects_a_non_number_and_keeps_the_prompt_open():
  message = FakeMessage(text="lots")
  state = await _prompted()
  broker = FakeUserBroker()

  await receive_max_orders(message, state, _account(), broker)

  assert broker.set_calls == []
  assert await state.get_state() == SetMaxOpenOrders.waiting_for_value.state


async def test_receive_rejects_a_negative_number():
  message = FakeMessage(text="-3")
  state = await _prompted()
  broker = FakeUserBroker()

  await receive_max_orders(message, state, _account(), broker)

  assert broker.set_calls == []
  assert "cannot be negative" in message.last


async def test_receive_reports_a_failed_write():
  message = FakeMessage(text="3")
  state = await _prompted()
  broker = FakeUserBroker(set_result=None)

  await receive_max_orders(message, state, _account(), broker)

  assert "Failed to update" in message.last
  assert await state.get_state() is None


async def test_cancel_clears_the_state_without_writing():
  message = FakeMessage(text="/cancel")
  state = await _prompted()

  await cancel_max_orders(message, state)

  assert await state.get_state() is None


# ── account pinning ─────────────────────────────────────────────────


async def test_receive_refuses_to_write_after_the_active_account_changed():
  """A /switch between the prompt and the reply would otherwise send the number
  to an account whose cap the user never saw — and this writes live risk
  config, not a page of trades."""
  message = FakeMessage(text="3")
  state = await _prompted("acc-1")
  broker = FakeUserBroker(set_result=_state_payload(3))

  await receive_max_orders(message, state, _account("acc-2"), broker)

  assert broker.set_calls == []
  assert "acc-1" in message.last and "acc-2" in message.last
  # State is cleared, so the stale prompt cannot fire again.
  assert await state.get_state() is None


async def test_the_account_is_checked_before_the_value_is_parsed():
  # An unparseable value normally keeps the prompt open to be retried; on the
  # wrong account there is nothing worth retrying.
  message = FakeMessage(text="lots")
  state = await _prompted("acc-1")
  broker = FakeUserBroker()

  await receive_max_orders(message, state, _account("acc-2"), broker)

  assert broker.set_calls == []
  assert await state.get_state() is None
