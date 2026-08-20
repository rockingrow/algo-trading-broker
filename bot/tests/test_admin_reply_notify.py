"""Tests for the /admin_private_reply_notify and /admin_public_reply_notify
handlers.

Each audience's broadcast reply-notice can be switched on/off; unlike the
other admin toggles (a tap on a button), these take an explicit
``enable``/``disable`` text argument, so the parsing and the no-argument
"show current + usage" path are what's covered here.
"""

from __future__ import annotations

from typing import Any, Optional

from aiogram.filters.command import CommandObject

from app.handlers.admin import cmd_private_reply_notify, cmd_public_reply_notify


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
    private_current: Optional[dict[str, Any]] = None,
    public_current: Optional[dict[str, Any]] = None,
    private_set_result: Optional[dict[str, Any]] = None,
    public_set_result: Optional[dict[str, Any]] = None,
  ):
    self._private_current = private_current
    self._public_current = public_current
    self._private_set_result = private_set_result
    self._public_set_result = public_set_result
    self.private_set_calls: list[bool] = []
    self.public_set_calls: list[bool] = []

  async def get_private_reply_notify(self) -> Optional[dict[str, Any]]:
    return self._private_current

  async def set_private_reply_notify(self, enabled: bool) -> Optional[dict[str, Any]]:
    self.private_set_calls.append(enabled)
    return self._private_set_result

  async def get_public_reply_notify(self) -> Optional[dict[str, Any]]:
    return self._public_current

  async def set_public_reply_notify(self, enabled: bool) -> Optional[dict[str, Any]]:
    self.public_set_calls.append(enabled)
    return self._public_set_result


def _cmd(name: str, args: Optional[str]) -> CommandObject:
  return CommandObject(command=name, args=args)


# ── /admin_private_reply_notify ──────────────────────────────────────


async def test_private_no_args_shows_current_state_and_usage():
  message = FakeMessage()
  broker = FakeAdminBroker(
    private_current={"setting": "private_broadcast_reply_notify", "state": "ENABLED"}
  )

  await cmd_private_reply_notify(
    message, _cmd("admin_private_reply_notify", None), broker
  )

  assert "ENABLED" in message.last
  assert "/admin_private_reply_notify enable" in message.last
  assert "/admin_private_reply_notify disable" in message.last
  assert broker.private_set_calls == []


async def test_private_no_args_broker_failure_shows_unknown():
  message = FakeMessage()
  broker = FakeAdminBroker(private_current=None)

  await cmd_private_reply_notify(
    message, _cmd("admin_private_reply_notify", ""), broker
  )

  assert "UNKNOWN" in message.last


async def test_private_enable_calls_setter_with_true():
  message = FakeMessage()
  broker = FakeAdminBroker(
    private_set_result={"setting": "private_broadcast_reply_notify", "state": "ENABLED"}
  )

  await cmd_private_reply_notify(
    message, _cmd("admin_private_reply_notify", "enable"), broker
  )

  assert broker.private_set_calls == [True]
  assert "ENABLED" in message.last


async def test_private_disable_calls_setter_with_false():
  message = FakeMessage()
  broker = FakeAdminBroker(
    private_set_result={
      "setting": "private_broadcast_reply_notify",
      "state": "DISABLED",
    }
  )

  await cmd_private_reply_notify(
    message, _cmd("admin_private_reply_notify", "DISABLE"), broker
  )

  assert broker.private_set_calls == [False]
  assert "DISABLED" in message.last


async def test_private_invalid_argument_is_rejected():
  message = FakeMessage()
  broker = FakeAdminBroker()

  await cmd_private_reply_notify(
    message, _cmd("admin_private_reply_notify", "sometimes"), broker
  )

  assert broker.private_set_calls == []
  assert "enable" in message.last and "disable" in message.last


async def test_private_setter_failure_surfaces_error():
  message = FakeMessage()
  broker = FakeAdminBroker(private_set_result=None)

  await cmd_private_reply_notify(
    message, _cmd("admin_private_reply_notify", "enable"), broker
  )

  assert "Failed to update" in message.last


# ── /admin_public_reply_notify ───────────────────────────────────────


async def test_public_no_args_shows_current_state():
  message = FakeMessage()
  broker = FakeAdminBroker(
    public_current={"setting": "public_broadcast_reply_notify", "state": "DISABLED"}
  )

  await cmd_public_reply_notify(message, _cmd("admin_public_reply_notify", None), broker)

  assert "DISABLED" in message.last
  assert broker.public_set_calls == []


async def test_public_enable_calls_setter_with_true():
  message = FakeMessage()
  broker = FakeAdminBroker(
    public_set_result={"setting": "public_broadcast_reply_notify", "state": "ENABLED"}
  )

  await cmd_public_reply_notify(
    message, _cmd("admin_public_reply_notify", "enable"), broker
  )

  assert broker.public_set_calls == [True]
  assert "ENABLED" in message.last


async def test_public_disable_calls_setter_with_false():
  message = FakeMessage()
  broker = FakeAdminBroker(
    public_set_result={"setting": "public_broadcast_reply_notify", "state": "DISABLED"}
  )

  await cmd_public_reply_notify(
    message, _cmd("admin_public_reply_notify", "disable"), broker
  )

  assert broker.public_set_calls == [False]
  assert "DISABLED" in message.last


async def test_public_invalid_argument_is_rejected():
  message = FakeMessage()
  broker = FakeAdminBroker()

  await cmd_public_reply_notify(
    message, _cmd("admin_public_reply_notify", "yes"), broker
  )

  assert broker.public_set_calls == []


async def test_public_setter_failure_surfaces_error():
  message = FakeMessage()
  broker = FakeAdminBroker(public_set_result=None)

  await cmd_public_reply_notify(
    message, _cmd("admin_public_reply_notify", "disable"), broker
  )

  assert "Failed to update" in message.last
