"""Tests for the /subscribe and /unsubscribe handlers.

The opt-in is per account and the broker decides which account that is (the
caller's active one), so the handlers must report the account the broker
echoed back — not guess one — and must not claim success when the broker call
failed.
"""

from __future__ import annotations

from typing import Any, Optional

from app.handlers.account import cmd_subscribe, cmd_unsubscribe


class FakeMessage:
  def __init__(self):
    self.answers: list[str] = []
    self.from_user = type("U", (), {"id": 777})()

  async def answer(self, text: str, reply_markup: Any = None, **kwargs: Any) -> None:
    self.answers.append(text)

  @property
  def last(self) -> str:
    return self.answers[-1]


class FakeBroker:
  def __init__(self, result: Optional[dict[str, Any]]):
    self._result = result
    self.calls: list[tuple[str, int]] = []

  async def subscribe_broadcast(self, telegram_user_id: int):
    self.calls.append(("subscribe", telegram_user_id))
    return self._result

  async def unsubscribe_broadcast(self, telegram_user_id: int):
    self.calls.append(("unsubscribe", telegram_user_id))
    return self._result


def _reply(subscribed: bool, account_id: str = "acc-2") -> dict[str, Any]:
  return {
    "subscribed": subscribed,
    "account_id": account_id,
    "market": "CRYPTO",
    "gateway": "BINANCE",
  }


async def test_subscribe_confirms_the_account_the_broker_applied_it_to():
  message, broker = FakeMessage(), FakeBroker(_reply(True))

  await cmd_subscribe(message, broker)

  assert broker.calls == [("subscribe", 777)]
  assert "Subscribed" in message.last
  assert "<code>acc-2</code>" in message.last


async def test_unsubscribe_confirms_the_account_the_broker_applied_it_to():
  message, broker = FakeMessage(), FakeBroker(_reply(False))

  await cmd_unsubscribe(message, broker)

  assert broker.calls == [("unsubscribe", 777)]
  assert "Unsubscribed" in message.last
  assert "<code>acc-2</code>" in message.last


async def test_subscribe_failure_is_reported_not_confirmed():
  message = FakeMessage()

  await cmd_subscribe(message, FakeBroker(None))

  assert "Failed to update subscription" in message.last
  assert "Subscribed" not in message.last


async def test_unsubscribe_failure_is_reported_not_confirmed():
  message = FakeMessage()

  await cmd_unsubscribe(message, FakeBroker(None))

  assert "Failed to update subscription" in message.last
  assert "Unsubscribed" not in message.last
