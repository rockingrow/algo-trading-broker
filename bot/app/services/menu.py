"""
app/services/menu.py — Owns every ``setMyCommands`` call.

Telegram stores a command menu per scope and keeps it until something
overwrites it, so the menu a user sees is state the bot has to maintain rather
than something it can compute at render time. ``CommandMenu`` maintains it:

- ``setup`` runs on startup and resets the *default* scope (what every chat the
  bot has never spoken to falls back to) to the /start-only menu — an unseen
  chat is by definition not linked yet.
- ``sync`` applies the chat-scoped menu for one user, and is what actually
  hides the user commands from someone who hasn't linked an account.
- ``refresh`` asks the broker for the current link status first. It is called
  on every update by ``middlewares/menu.py`` (so the menu also corrects itself
  after a change the bot never saw, e.g. an /admin_rotate that unlinked the
  user) and again right after a link/unlink, so the menu changes with the same
  tap rather than on the user's next message.

The menu last applied to each chat is remembered in-process, so the steady
state — a linked user sending messages — costs no Telegram calls at all. The
cache starts empty on boot, which is what re-applies menus after a release that
changes the command lists.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommandScopeChat, BotCommandScopeDefault

from app.commands import START_ONLY_COMMANDS, menu_for
from app.logger import get_logger
from app.services.broker_client import BrokerClientUser

log = get_logger(__name__)


class CommandMenu:
  def __init__(self, admin_ids: set[int]) -> None:
    self._admin_ids = set(admin_ids)
    # chat id → the command names last applied there.
    self._applied: dict[int, tuple[str, ...]] = {}

  async def setup(self, bot: Bot, broker: BrokerClientUser) -> None:
    """Startup: reset the default menu, then refresh each admin's own menu."""
    await bot.set_my_commands(START_ONLY_COMMANDS, scope=BotCommandScopeDefault())
    self._applied.clear()

    for admin_id in self._admin_ids:
      await self.refresh(bot, broker, admin_id)

    log.info(
      "Command menus set — default=%d cmds (start only), admins=%d",
      len(START_ONLY_COMMANDS),
      len(self._admin_ids),
    )

  async def refresh(self, bot: Bot, broker: BrokerClientUser, user_id: int) -> None:
    """Re-read the user's link status from the broker and apply the matching menu.

    A broker that can't be reached leaves the menu alone: an outage is not
    evidence that the user unlinked, and wiping their commands over it would
    only add confusion to a bot that is already answering nothing.
    """
    answered, account = await broker.resolve_account(user_id)
    if answered:
      await self.sync(bot, user_id, linked=account is not None)

  async def sync(self, bot: Bot, user_id: int, *, linked: bool) -> None:
    """Apply the menu for *user_id*, unless it is already the one in place."""
    commands = menu_for(linked=linked, is_admin=user_id in self._admin_ids)
    applied = tuple(c.command for c in commands)
    if self._applied.get(user_id) == applied:
      return

    try:
      await bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id=user_id))
    except TelegramAPIError as exc:
      # Telegram answers "chat not found" until the user has messaged the bot
      # at least once (typical for a configured admin who never started it).
      # Nothing is cached, so the next update retries. The menu is cosmetic —
      # never let it take the update down with it.
      log.warning("Skip command menu for %s: %s", user_id, exc)
      return

    self._applied[user_id] = applied
    log.debug(
      "Command menu for %s → %d cmds (linked=%s)", user_id, len(commands), linked
    )
