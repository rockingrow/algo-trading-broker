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
  BotCommand(command="myaccounts", description="List linked accounts"),
  BotCommand(command="link", description="Add another account"),
  BotCommand(command="switch", description="Change active account"),
  BotCommand(command="unlink", description="Unlink active account"),
  BotCommand(command="subscribe", description="Get completed-trade alerts"),
  BotCommand(command="unsubscribe", description="Stop completed-trade alerts"),
  BotCommand(command="help", description="Help"),
]

# Visual separator between the user commands and the admin commands in the
# admin menu. Telegram command names may only contain [a-z0-9_] (a literal
# "-----—" divider isn't a valid command), so the divider is a real command —
# ``/admin_help``, which lists the admin commands — carrying a dashed
# description that reads as a section header in the menu.
ADMIN_DIVIDER = BotCommand(
  command="admin_help", description="───────── ADMIN ─────────"
)

# Admin commands are prefixed ``admin_`` so they group under the divider and
# read as a distinct set. The handlers also accept the legacy un-prefixed names
# (see app/handlers/admin.py) so existing muscle memory keeps working; only the
# prefixed form is advertised in the menu.
ADMIN_EXTRA_COMMANDS = [
  ADMIN_DIVIDER,
  BotCommand(command="admin_accounts", description="[ADMIN] Account list"),
  BotCommand(command="admin_newaccount", description="[ADMIN] Register a new account"),
  BotCommand(command="admin_trades", description="[ADMIN] Trades for an account"),
  BotCommand(command="admin_flat", description="[ADMIN] FLAT system-wide / account"),
  BotCommand(command="admin_rotate", description="[ADMIN] Rotate token + unlink users"),
  BotCommand(command="admin_settings", description="[ADMIN] Broker settings"),
  BotCommand(command="admin_linkaccount", description="[ADMIN] Link a Telegram user"),
  BotCommand(command="admin_invite_url", description="[ADMIN] One-tap invite link"),
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
