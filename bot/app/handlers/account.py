"""
app/handlers/account.py — /status and /unlink (protected router).
"""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app import emojis
from app.formatters import messages
from app.helpers import safe_edit_text
from app.keyboards import inline
from app.services.broker_client import BrokerClient

router = Router(name="account")


@router.message(Command("status"))
async def cmd_status(message: Message, account: dict[str, Any]) -> None:
  await message.answer(messages.format_account(account))


@router.message(Command("unlink"))
async def cmd_unlink(message: Message, account: dict[str, Any]) -> None:
  await message.answer(
    "Are you sure you want to <b>unlink</b> your account from Telegram?",
    reply_markup=inline.confirm_keyboard("unlink"),
  )


@router.callback_query(F.data == "unlink:confirm")
async def cb_unlink(call: CallbackQuery, broker: BrokerClient) -> None:
  ok = await broker.unlink(call.from_user.id)
  await safe_edit_text(
    call.message,
    f"{emojis.CHECK} Unlinked. Type /start to link again."
    if ok
    else f"{emojis.CROSS} Unlink failed. Try again later.",
  )
  await call.answer()


@router.callback_query(F.data == "unlink:cancel")
async def cb_unlink_cancel(call: CallbackQuery) -> None:
  await safe_edit_text(call.message, "Cancelled.")
  await call.answer()
