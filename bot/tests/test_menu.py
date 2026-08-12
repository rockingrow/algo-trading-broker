"""Tests for the link-aware command menu — the service, the middleware that
drives it on every update, and the /help gate that goes with it."""

from __future__ import annotations

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SetMyCommands
from aiogram.types import BotCommandScopeChat, BotCommandScopeDefault

from app.commands import ADMIN_COMMANDS, ADMIN_EXTRA_COMMANDS, USER_COMMANDS
from app.handlers import get_routers, start
from app.middlewares.auth import AuthMiddleware
from app.middlewares.menu import CommandMenuMiddleware
from app.services.menu import CommandMenu

ACCOUNT = {"account_id": "acc-1", "is_active": True}


class FakeBot:
  """Records set_my_commands calls; can be told to fail for given chat ids."""

  def __init__(self, fail_ids=()):
    self.calls = []  # (command names, scope)
    self._fail_ids = set(fail_ids)

  async def set_my_commands(self, commands, scope=None):
    chat_id = getattr(scope, "chat_id", None)
    if chat_id in self._fail_ids:
      raise TelegramBadRequest(
        method=SetMyCommands(commands=commands), message="Bad Request: chat not found"
      )
    self.calls.append(([c.command for c in commands], scope))

  @property
  def chat_calls(self):
    return [c for c in self.calls if isinstance(c[1], BotCommandScopeChat)]

  @property
  def default_calls(self):
    return [c for c in self.calls if isinstance(c[1], BotCommandScopeDefault)]


class FakeBroker:
  """``resolve_account`` mirrors the real client's (answered, account) contract."""

  def __init__(self, accounts=None, *, reachable=True):
    self.accounts = accounts or {}  # telegram id → account
    self.reachable = reachable
    self.calls = []

  async def resolve_account(self, telegram_user_id):
    self.calls.append(telegram_user_id)
    if not self.reachable:
      return False, None
    return True, self.accounts.get(telegram_user_id)

  async def get_account(self, telegram_user_id):
    _, account = await self.resolve_account(telegram_user_id)
    return account


class FakeUser:
  def __init__(self, user_id=4242, is_bot=False):
    self.id = user_id
    self.is_bot = is_bot


class FakeChat:
  def __init__(self, chat_type="private"):
    self.type = chat_type


def _data(user=None, broker=None, bot=None, chat=None):
  data = {"bot": bot or FakeBot(), "broker": broker or FakeBroker()}
  if user is not None:
    data["event_from_user"] = user
  data["event_chat"] = FakeChat() if chat is None else chat
  return data


# ── CommandMenu.sync ────────────────────────────────────────────────


async def test_sync_gives_an_unlinked_user_start_only():
  bot, menu = FakeBot(), CommandMenu(set())

  await menu.sync(bot, 4242, linked=False)

  names, scope = bot.chat_calls[0]
  assert names == ["start"]
  assert scope.chat_id == 4242


async def test_sync_gives_a_linked_user_the_full_menu():
  bot, menu = FakeBot(), CommandMenu(set())

  await menu.sync(bot, 4242, linked=True)

  assert bot.chat_calls[0][0] == [c.command for c in USER_COMMANDS]


async def test_sync_keeps_the_admin_half_for_an_unlinked_admin():
  bot, menu = FakeBot(), CommandMenu({7})

  await menu.sync(bot, 7, linked=False)

  assert bot.chat_calls[0][0] == ["start"] + [c.command for c in ADMIN_EXTRA_COMMANDS]


async def test_sync_skips_telegram_when_the_menu_is_already_in_place():
  bot, menu = FakeBot(), CommandMenu(set())

  await menu.sync(bot, 4242, linked=False)
  await menu.sync(bot, 4242, linked=False)
  await menu.sync(bot, 4242, linked=False)

  assert len(bot.calls) == 1


async def test_sync_reapplies_when_the_link_status_changes():
  bot, menu = FakeBot(), CommandMenu(set())

  await menu.sync(bot, 4242, linked=False)
  await menu.sync(bot, 4242, linked=True)
  await menu.sync(bot, 4242, linked=False)

  assert [names for names, _ in bot.chat_calls] == [
    ["start"],
    [c.command for c in USER_COMMANDS],
    ["start"],
  ]


async def test_sync_caches_per_user():
  bot, menu = FakeBot(), CommandMenu(set())

  await menu.sync(bot, 111, linked=True)
  await menu.sync(bot, 222, linked=True)

  assert [scope.chat_id for _, scope in bot.chat_calls] == [111, 222]


async def test_sync_survives_an_unreachable_chat_and_retries_later():
  bot, menu = FakeBot(fail_ids={999}), CommandMenu(set())

  # Telegram says "chat not found" until the user has messaged the bot once.
  await menu.sync(bot, 999, linked=True)
  assert bot.calls == []

  # The failure isn't cached as applied, so the next attempt tries again.
  bot._fail_ids.clear()
  await menu.sync(bot, 999, linked=True)
  assert len(bot.chat_calls) == 1


# ── CommandMenu.refresh / setup ─────────────────────────────────────


async def test_refresh_reads_the_link_status_from_the_broker():
  bot, menu = FakeBot(), CommandMenu(set())
  broker = FakeBroker({4242: ACCOUNT})

  await menu.refresh(bot, broker, 4242)
  await menu.refresh(bot, broker, 111)

  assert bot.chat_calls[0][0] == [c.command for c in USER_COMMANDS]
  assert bot.chat_calls[1][0] == ["start"]


async def test_refresh_leaves_the_menu_alone_when_the_broker_is_down():
  bot, menu = FakeBot(), CommandMenu(set())

  await menu.refresh(bot, FakeBroker(reachable=False), 4242)

  # An outage is not evidence that the user unlinked.
  assert bot.calls == []


async def test_setup_defaults_every_unseen_chat_to_start_only():
  bot, menu = FakeBot(), CommandMenu(set())

  await menu.setup(bot, FakeBroker())

  assert len(bot.default_calls) == 1
  assert bot.default_calls[0][0] == ["start"]
  assert bot.chat_calls == []


async def test_setup_refreshes_each_admin_menu():
  bot, menu = FakeBot(), CommandMenu({111, 222})
  broker = FakeBroker({111: ACCOUNT})

  await menu.setup(bot, broker)

  by_chat = {scope.chat_id: names for names, scope in bot.chat_calls}
  assert by_chat[111] == [c.command for c in ADMIN_COMMANDS]
  # 222 hasn't linked an account, so only the admin half is advertised.
  assert by_chat[222] == ["start"] + [c.command for c in ADMIN_EXTRA_COMMANDS]


async def test_setup_survives_an_unreachable_admin():
  bot, menu = FakeBot(fail_ids={999}), CommandMenu({999})

  await menu.setup(bot, FakeBroker())  # must not raise

  assert len(bot.calls) == 1  # the default scope only
  assert isinstance(bot.calls[0][1], BotCommandScopeDefault)


# ── CommandMenuMiddleware ───────────────────────────────────────────


async def _run(middleware, data, event=object()):
  """Call the middleware, recording whether the handler was reached."""
  seen = []

  async def handler(event, data):
    seen.append(data)
    return "handled"

  result = await middleware(handler, event, data)
  return result, seen


async def test_middleware_hides_the_commands_of_an_unlinked_user():
  bot, broker = FakeBot(), FakeBroker()
  data = _data(FakeUser(), broker, bot)

  result, seen = await _run(CommandMenuMiddleware(CommandMenu(set())), data)

  assert bot.chat_calls[0][0] == ["start"]
  # The update is still handled — this middleware only maintains the menu.
  assert result == "handled" and len(seen) == 1


async def test_middleware_restores_the_commands_of_a_linked_user():
  bot, broker = FakeBot(), FakeBroker({4242: ACCOUNT})
  data = _data(FakeUser(), broker, bot)

  await _run(CommandMenuMiddleware(CommandMenu(set())), data)

  assert bot.chat_calls[0][0] == [c.command for c in USER_COMMANDS]


async def test_middleware_passes_the_account_on_to_authmiddleware():
  broker = FakeBroker({4242: ACCOUNT})
  data = _data(FakeUser(), broker)

  await _run(CommandMenuMiddleware(CommandMenu(set())), data)

  assert data["account"] == ACCOUNT
  # …and the menu itself, so handlers can re-sync after a link/unlink.
  assert isinstance(data["menu"], CommandMenu)


async def test_middleware_does_not_touch_the_menu_when_the_broker_is_down():
  bot = FakeBot()
  data = _data(FakeUser(), FakeBroker(reachable=False), bot)

  result, _ = await _run(CommandMenuMiddleware(CommandMenu(set())), data)

  assert bot.calls == []
  assert "account" not in data
  assert result == "handled"


async def test_middleware_ignores_updates_without_a_user():
  bot, broker = FakeBot(), FakeBroker()
  data = _data(None, broker, bot)

  result, _ = await _run(CommandMenuMiddleware(CommandMenu(set())), data)

  assert broker.calls == [] and bot.calls == []
  assert result == "handled"
  # Handlers still get the menu even when there is nobody to sync.
  assert isinstance(data["menu"], CommandMenu)


async def test_middleware_skips_the_menu_outside_private_chats():
  bot, broker = FakeBot(), FakeBroker({4242: ACCOUNT})
  data = _data(FakeUser(), broker, bot, chat=FakeChat("group"))

  await _run(CommandMenuMiddleware(CommandMenu(set())), data)

  # A chat-scoped menu aimed at a group member's private chat is pointless
  # (and often "chat not found"), but the account still resolves for handlers.
  assert bot.calls == []
  assert data["account"] == ACCOUNT


# ── /help is gated with the rest ────────────────────────────────────


async def test_help_router_requires_a_linked_account():
  routers = get_routers()

  assert start.help_router in routers
  assert any(
    isinstance(m, AuthMiddleware) for m in start.help_router.message.middleware
  )
  # /start stays public — it is the one command an unlinked user is offered.
  assert not any(isinstance(m, AuthMiddleware) for m in start.router.message.middleware)


async def test_authmiddleware_reuses_the_account_the_menu_already_resolved():
  broker = FakeBroker({4242: ACCOUNT})
  data = _data(FakeUser(), broker)
  data["account"] = ACCOUNT

  result, _ = await _run(AuthMiddleware(), data)

  assert result == "handled"
  assert broker.calls == []  # no second round-trip for the same answer


async def test_authmiddleware_turns_away_an_unlinked_user():
  data = _data(FakeUser(), FakeBroker())

  result, seen = await _run(AuthMiddleware(), data)

  # Propagation stops, so no protected handler ever sees the update. (Which
  # reply it sends depends on the event type — see AuthMiddleware._deny.)
  assert result is None and seen == []
