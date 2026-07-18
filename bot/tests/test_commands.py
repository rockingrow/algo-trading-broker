"""Tests for command lists and scoped registration."""

from __future__ import annotations

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SetMyCommands
from aiogram.types import BotCommandScopeChat, BotCommandScopeDefault

from app.commands import (
  ADMIN_COMMANDS,
  ADMIN_EXTRA_COMMANDS,
  USER_COMMANDS,
  setup_bot_commands,
)


def test_command_list_sizes():
  assert len(USER_COMMANDS) == 10
  assert len(ADMIN_EXTRA_COMMANDS) == 6
  # Admin sees user commands plus the extras.
  assert len(ADMIN_COMMANDS) == len(USER_COMMANDS) + len(ADMIN_EXTRA_COMMANDS)
  user_names = {c.command for c in USER_COMMANDS}
  assert user_names.issubset({c.command for c in ADMIN_COMMANDS})
  assert {"accounts", "newaccount", "atrades", "aflat", "rotate", "settings"} <= {
    c.command for c in ADMIN_COMMANDS
  }


class FakeBot:
  def __init__(self, fail_ids=()):
    self.calls = []  # list of (commands, scope)
    self._fail_ids = set(fail_ids)

  async def set_my_commands(self, commands, scope=None):
    chat_id = getattr(scope, "chat_id", None)
    if chat_id in self._fail_ids:
      raise TelegramBadRequest(
        method=SetMyCommands(commands=commands), message="Bad Request: chat not found"
      )
    self.calls.append((commands, scope))


async def test_setup_bot_commands_registers_default_and_admin_scopes():
  bot = FakeBot()
  await setup_bot_commands(bot, {111, 222})

  default = [c for c in bot.calls if isinstance(c[1], BotCommandScopeDefault)]
  chats = [c for c in bot.calls if isinstance(c[1], BotCommandScopeChat)]

  assert len(default) == 1
  assert len(default[0][0]) == len(USER_COMMANDS)
  assert len(chats) == 2
  assert {c[1].chat_id for c in chats} == {111, 222}
  assert all(len(c[0]) == len(ADMIN_COMMANDS) for c in chats)


async def test_setup_bot_commands_survives_unreachable_admin():
  bot = FakeBot(fail_ids={999})
  # Must not raise even though the admin chat is unreachable.
  await setup_bot_commands(bot, {999})
  # Only the default-scope call was recorded; the admin call raised + was caught.
  assert len(bot.calls) == 1
  assert isinstance(bot.calls[0][1], BotCommandScopeDefault)


async def test_setup_bot_commands_no_admins():
  bot = FakeBot()
  await setup_bot_commands(bot, set())
  assert len(bot.calls) == 1  # default scope only
