"""
app/commands.py — Command lists + per-scope registration.

Two menus:
- USER_COMMANDS  → default scope (every private chat / enduser).
- ADMIN_COMMANDS → chat scope, only for configured admin ids (user cmds + extras).

``setup_bot_commands`` runs on every startup, so the menus are re-initialised
each time the bot boots (and whenever admin ids change in the env).
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from app.logger import get_logger

log = get_logger(__name__)

USER_COMMANDS = [
  BotCommand(command="start", description="Link account"),
  BotCommand(command="trades", description="Recent trades"),
  BotCommand(command="flat", description="Close all positions"),
  BotCommand(command="prevent", description="Block new orders"),
  BotCommand(command="allow", description="Allow new orders"),
  BotCommand(command="status", description="Account info"),
  BotCommand(command="link", description="Add another account"),
  BotCommand(command="switch", description="Change active account"),
  BotCommand(command="unlink", description="Unlink active account"),
  BotCommand(command="help", description="Help"),
]

ADMIN_EXTRA_COMMANDS = [
  BotCommand(command="accounts", description="[ADMIN] Account list"),
  BotCommand(command="newaccount", description="[ADMIN] Register a new account"),
  BotCommand(command="atrades", description="[ADMIN] Trades for an account"),
  BotCommand(command="aflat", description="[ADMIN] FLAT system-wide / account"),
  BotCommand(command="rotate", description="[ADMIN] Rotate link token"),
  BotCommand(command="settings", description="[ADMIN] Broker settings"),
]

ADMIN_COMMANDS = USER_COMMANDS + ADMIN_EXTRA_COMMANDS


async def setup_bot_commands(bot: Bot, admin_ids: set[int]) -> None:
  """(Re)register command menus: default (enduser) + chat-scoped admin menus."""
  await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())

  for admin_id in admin_ids:
    try:
      await bot.set_my_commands(
        ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id)
      )
    except TelegramBadRequest as exc:
      # Telegram returns "chat not found" until the admin has messaged the bot
      # at least once; the menu applies after their first /start + next restart.
      log.warning("Skip admin menu for %s (chat not reachable yet): %s", admin_id, exc)

  log.info(
    "Command menus set — default=%d cmds, admins=%d (each %d cmds)",
    len(USER_COMMANDS),
    len(admin_ids),
    len(ADMIN_COMMANDS),
  )
