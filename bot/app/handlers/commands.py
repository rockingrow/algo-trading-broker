"""
app/handlers/commands.py — Trading control: /flat, /prevent, /allow.

Each command shows a confirmation keyboard; the actual broker call happens only
after the user taps "Xác nhận". Protected router (requires a linked account).
"""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.formatters import messages
from app.keyboards import inline
from app.services.broker_client import BrokerClient

router = Router(name="commands")


@router.message(Command("flat"))
async def cmd_flat(message: Message, account: dict[str, Any]) -> None:
  await message.answer(
    f"⚠️ Xác nhận <b>FLAT</b> (đóng toàn bộ vị thế) cho tài khoản "
    f"<code>{account.get('account_id')}</code>?",
    reply_markup=inline.confirm_keyboard("flat"),
  )


@router.message(Command("prevent"))
async def cmd_prevent(message: Message, account: dict[str, Any]) -> None:
  await message.answer(
    f"⚠️ Xác nhận <b>CHẶN</b> vào lệnh mới cho tài khoản "
    f"<code>{account.get('account_id')}</code>?",
    reply_markup=inline.confirm_keyboard("prevent"),
  )


@router.message(Command("allow"))
async def cmd_allow(message: Message, account: dict[str, Any]) -> None:
  await message.answer(
    f"Xác nhận <b>CHO PHÉP</b> vào lệnh mới cho tài khoản "
    f"<code>{account.get('account_id')}</code>?",
    reply_markup=inline.confirm_keyboard("allow"),
  )


@router.callback_query(F.data.in_({"flat:confirm", "prevent:confirm", "allow:confirm"}))
async def cb_confirm(call: CallbackQuery, broker: BrokerClient) -> None:
  action = call.data.split(":", 1)[0]
  tg_id = call.from_user.id

  if action == "flat":
    result = await broker.flat(tg_id)
  elif action == "prevent":
    result = await broker.prevent(tg_id, enabled=True)
  else:  # allow
    result = await broker.prevent(tg_id, enabled=False)

  if result is None:
    await call.message.edit_text("❌ Lệnh thất bại. Thử lại sau.")
  else:
    await call.message.edit_text(messages.format_command_result(result))
  await call.answer()


@router.callback_query(F.data.in_({"flat:cancel", "prevent:cancel", "allow:cancel"}))
async def cb_cancel(call: CallbackQuery) -> None:
  await call.message.edit_text("Đã hủy.")
  await call.answer()
