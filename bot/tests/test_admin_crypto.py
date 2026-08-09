"""Tests for the /admin_crypto_symbols and /admin_crypto_leverage handlers.

Cover the read-current + prompt path, the happy-path submit, and the input
validation the bot enforces locally so the broker doesn't have to reject an
obvious typo just to bounce it back to the user.
"""

from __future__ import annotations

from typing import Any, Optional

from aiogram.filters.command import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage, StorageKey

from app.handlers.admin import (
  cancel_admin_crypto_leverage,
  cancel_admin_crypto_symbols,
  cmd_admin_crypto_leverage,
  cmd_admin_crypto_symbols,
  receive_admin_crypto_leverage,
  receive_admin_crypto_symbols,
)
from app.states import AdminCryptoAllowedSymbol, AdminCryptoMaxLeverage


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
    symbols: Optional[dict[str, Any]] = None,
    leverage: Optional[dict[str, Any]] = None,
    set_symbols_result: Optional[dict[str, Any]] = None,
    set_leverage_result: Optional[dict[str, Any]] = None,
  ):
    self._symbols = symbols
    self._leverage = leverage
    self._set_symbols_result = set_symbols_result
    self._set_leverage_result = set_leverage_result
    self.set_symbols_calls: list[list[str]] = []
    self.set_leverage_calls: list[int] = []

  async def get_crypto_allowed_symbol(self) -> Optional[dict[str, Any]]:
    return self._symbols

  async def get_crypto_max_leverage(self) -> Optional[dict[str, Any]]:
    return self._leverage

  async def set_crypto_allowed_symbol(self, symbols: list[str]) -> Optional[dict[str, Any]]:
    self.set_symbols_calls.append(symbols)
    return self._set_symbols_result

  async def set_crypto_max_leverage(self, default_leverage: int) -> Optional[dict[str, Any]]:
    self.set_leverage_calls.append(default_leverage)
    return self._set_leverage_result


def _make_state() -> FSMContext:
  storage = MemoryStorage()
  key = StorageKey(bot_id=1, chat_id=1, user_id=1)
  return FSMContext(storage=storage, key=key)


# ── /admin_crypto_symbols ───────────────────────────────────────────


async def test_cmd_crypto_symbols_shows_current_and_prompts():
  message = FakeMessage()
  state = _make_state()
  broker = FakeAdminBroker(symbols={"setting": "crypto_allowed_symbol", "value": "BTC,ETH"})

  await cmd_admin_crypto_symbols(message, state, broker)

  assert "BTC,ETH" in message.last
  assert "comma-separated" in message.last
  assert await state.get_state() == AdminCryptoAllowedSymbol.waiting_for_symbols.state


async def test_cmd_crypto_symbols_unset_shows_placeholder():
  message = FakeMessage()
  state = _make_state()
  broker = FakeAdminBroker(symbols={"setting": "crypto_allowed_symbol", "value": ""})

  await cmd_admin_crypto_symbols(message, state, broker)

  assert "(unset)" in message.last


async def test_cmd_crypto_symbols_broker_failure_no_state():
  message = FakeMessage()
  state = _make_state()
  broker = FakeAdminBroker(symbols=None)

  await cmd_admin_crypto_symbols(message, state, broker)

  assert "Failed to fetch" in message.last
  assert await state.get_state() is None


async def test_receive_crypto_symbols_forwards_list_and_clears_state():
  message = FakeMessage(text="btc, eth , sol")
  state = _make_state()
  await state.set_state(AdminCryptoAllowedSymbol.waiting_for_symbols)
  broker = FakeAdminBroker(
    set_symbols_result={"setting": "crypto_allowed_symbol", "value": "BTC,ETH,SOL"}
  )

  await receive_admin_crypto_symbols(message, state, broker)

  assert broker.set_symbols_calls == [["btc", "eth", "sol"]]
  assert "BTC,ETH,SOL" in message.last
  assert await state.get_state() is None


async def test_receive_crypto_symbols_rejects_blank_input_stays_in_state():
  message = FakeMessage(text="  , , ")
  state = _make_state()
  await state.set_state(AdminCryptoAllowedSymbol.waiting_for_symbols)
  broker = FakeAdminBroker()

  await receive_admin_crypto_symbols(message, state, broker)

  assert broker.set_symbols_calls == []
  assert "at least one symbol" in message.last
  assert await state.get_state() == AdminCryptoAllowedSymbol.waiting_for_symbols.state


async def test_receive_crypto_symbols_broker_failure_surfaces_error():
  message = FakeMessage(text="btc")
  state = _make_state()
  await state.set_state(AdminCryptoAllowedSymbol.waiting_for_symbols)
  broker = FakeAdminBroker(set_symbols_result=None)

  await receive_admin_crypto_symbols(message, state, broker)

  assert "Failed to update" in message.last
  assert await state.get_state() is None


async def test_cancel_crypto_symbols_clears_state():
  message = FakeMessage()
  state = _make_state()
  await state.set_state(AdminCryptoAllowedSymbol.waiting_for_symbols)

  await cancel_admin_crypto_symbols(message, state)

  assert "Cancelled" in message.last
  assert await state.get_state() is None


# ── /admin_crypto_leverage ──────────────────────────────────────────


async def test_cmd_crypto_leverage_shows_current_and_prompts():
  message = FakeMessage()
  state = _make_state()
  broker = FakeAdminBroker(leverage={"setting": "crypto_max_leverage", "value": "10"})

  await cmd_admin_crypto_leverage(message, state, broker)

  assert "10" in message.last
  assert "positive integer" in message.last
  assert await state.get_state() == AdminCryptoMaxLeverage.waiting_for_leverage.state


async def test_cmd_crypto_leverage_broker_failure_no_state():
  message = FakeMessage()
  state = _make_state()
  broker = FakeAdminBroker(leverage=None)

  await cmd_admin_crypto_leverage(message, state, broker)

  assert "Failed to fetch" in message.last
  assert await state.get_state() is None


async def test_receive_crypto_leverage_forwards_int_and_clears_state():
  message = FakeMessage(text="20")
  state = _make_state()
  await state.set_state(AdminCryptoMaxLeverage.waiting_for_leverage)
  broker = FakeAdminBroker(
    set_leverage_result={"setting": "crypto_max_leverage", "value": "20"}
  )

  await receive_admin_crypto_leverage(message, state, broker)

  assert broker.set_leverage_calls == [20]
  assert "20" in message.last
  assert await state.get_state() is None


async def test_receive_crypto_leverage_rejects_non_integer():
  message = FakeMessage(text="not-a-number")
  state = _make_state()
  await state.set_state(AdminCryptoMaxLeverage.waiting_for_leverage)
  broker = FakeAdminBroker()

  await receive_admin_crypto_leverage(message, state, broker)

  assert broker.set_leverage_calls == []
  assert "integer" in message.last
  assert await state.get_state() == AdminCryptoMaxLeverage.waiting_for_leverage.state


async def test_receive_crypto_leverage_rejects_zero_and_negative():
  broker = FakeAdminBroker()

  for text in ("0", "-5"):
    message = FakeMessage(text=text)
    state = _make_state()
    await state.set_state(AdminCryptoMaxLeverage.waiting_for_leverage)

    await receive_admin_crypto_leverage(message, state, broker)

    assert broker.set_leverage_calls == []
    assert "positive integer" in message.last
    assert await state.get_state() == AdminCryptoMaxLeverage.waiting_for_leverage.state


async def test_receive_crypto_leverage_broker_failure_surfaces_error():
  message = FakeMessage(text="15")
  state = _make_state()
  await state.set_state(AdminCryptoMaxLeverage.waiting_for_leverage)
  broker = FakeAdminBroker(set_leverage_result=None)

  await receive_admin_crypto_leverage(message, state, broker)

  assert "Failed to update" in message.last
  assert await state.get_state() is None


async def test_cancel_crypto_leverage_clears_state():
  message = FakeMessage()
  state = _make_state()
  await state.set_state(AdminCryptoMaxLeverage.waiting_for_leverage)

  await cancel_admin_crypto_leverage(message, state)

  assert "Cancelled" in message.last
  assert await state.get_state() is None
