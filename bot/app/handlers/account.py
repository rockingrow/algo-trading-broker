"""
app/handlers/account.py — /status and /unlink (protected router).
"""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

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
    "Bạn có chắc muốn <b>hủy liên kết</b> tài khoản khỏi Telegram?",
    reply_markup=inline.confirm_keyboard("unlink"),
  )


@router.callback_query(F.data == "unlink:confirm")
async def cb_unlink(call: CallbackQuery, broker: BrokerClient) -> None:
  ok = await broker.unlink(call.from_user.id)
  await safe_edit_text(
    call.message,
    "✅ Đã hủy liên kết. Gõ /start để liên kết lại."
    if ok
    else "❌ Hủy liên kết thất bại. Thử lại sau.",
  )
  await call.answer()


@router.callback_query(F.data == "unlink:cancel")
async def cb_unlink_cancel(call: CallbackQuery) -> None:
  await safe_edit_text(call.message, "Đã hủy.")
  await call.answer()
