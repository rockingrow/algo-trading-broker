"""Tests for the /admin_public_chats handler.

The public broadcast audience lives in a broker setting rather than the .env,
so it is edited from here. Covers reading the current value, submitting a new
list, and the explicit "turn it off" answer.
"""

from __future__ import annotations

from typing import Any, Optional

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage, StorageKey

from app.handlers.admin import (
  cancel_admin_public_chats,
  cmd_admin_public_chats,
  receive_admin_public_chats,
)
from app.states import AdminPublicBroadcastChats


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
  ):
    self._current = current
    self._set_result = set_result
    self.set_calls: list[list[str]] = []

  async def get_public_broadcast_chat_ids(self) -> Optional[dict[str, Any]]:
    return self._current

  async def set_public_broadcast_chat_ids(
    self, chat_ids: list[str]
  ) -> Optional[dict[str, Any]]:
    self.set_calls.append(chat_ids)
    return self._set_result


def _make_state() -> FSMContext:
  storage = MemoryStorage()
  key = StorageKey(bot_id=1, chat_id=1, user_id=1)
  return FSMContext(storage=storage, key=key)


async def test_cmd_shows_current_and_prompts():
  message = FakeMessage()
  state = _make_state()
  broker = FakeAdminBroker(
    current={"setting": "public_broadcast_chat_ids", "value": "-100,-200"}
  )

  await cmd_admin_public_chats(message, state, broker)

  assert "-100,-200" in message.last
  assert "comma-separated" in message.last
  assert await state.get_state() == AdminPublicBroadcastChats.waiting_for_chat_ids.state


async def test_cmd_shows_off_when_unset():
  message = FakeMessage()
  broker = FakeAdminBroker(
    current={"setting": "public_broadcast_chat_ids", "value": ""}
  )

  await cmd_admin_public_chats(message, _make_state(), broker)

  assert "public broadcast off" in message.last


async def test_cmd_reports_a_failed_read():
  message = FakeMessage()
  state = _make_state()

  await cmd_admin_public_chats(message, state, FakeAdminBroker(current=None))

  assert "Failed to fetch" in message.last
  assert await state.get_state() is None


async def test_receive_submits_the_split_list():
  message = FakeMessage(" -1001234567890 , @my_channel ")
  state = _make_state()
  broker = FakeAdminBroker(
    set_result={
      "setting": "public_broadcast_chat_ids",
      "value": "-1001234567890,@my_channel",
    }
  )

  await receive_admin_public_chats(message, state, broker)

  assert broker.set_calls == [["-1001234567890", "@my_channel"]]
  assert "updated" in message.last
  assert await state.get_state() is None


async def test_a_single_dash_turns_the_public_broadcast_off():
  message = FakeMessage("-")
  broker = FakeAdminBroker(set_result={"value": ""})

  await receive_admin_public_chats(message, _make_state(), broker)

  assert broker.set_calls == [[]]
  assert "public broadcast off" in message.last


async def test_receive_reports_a_failed_write():
  message = FakeMessage("-100")
  state = _make_state()
  broker = FakeAdminBroker(set_result=None)

  await receive_admin_public_chats(message, state, broker)

  assert "Failed to update" in message.last
  assert await state.get_state() is None


async def test_cancel_clears_the_state():
  message = FakeMessage()
  state = _make_state()
  await state.set_state(AdminPublicBroadcastChats.waiting_for_chat_ids)

  await cancel_admin_public_chats(message, state)

  assert await state.get_state() is None
  assert "Cancelled" in message.last
