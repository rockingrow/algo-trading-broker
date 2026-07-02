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
  BotCommand(command="start", description="Liên kết tài khoản"),
  BotCommand(command="trades", description="Giao dịch gần đây"),
  BotCommand(command="flat", description="Đóng toàn bộ vị thế"),
  BotCommand(command="prevent", description="Chặn vào lệnh mới"),
  BotCommand(command="allow", description="Cho phép vào lệnh mới"),
  BotCommand(command="status", description="Thông tin tài khoản"),
  BotCommand(command="unlink", description="Hủy liên kết"),
  BotCommand(command="help", description="Trợ giúp"),
]

ADMIN_EXTRA_COMMANDS = [
  BotCommand(command="accounts", description="[ADMIN] Danh sách tài khoản"),
  BotCommand(command="atrades", description="[ADMIN] Trades của một tài khoản"),
  BotCommand(command="aflat", description="[ADMIN] FLAT toàn hệ thống / tài khoản"),
  BotCommand(command="rotate", description="[ADMIN] Xoay link token"),
  BotCommand(command="settings", description="[ADMIN] Cài đặt broker"),
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
