"""
app/handlers/commands.py — Trading control: /flat, /prevent, /allow.

Each command shows a confirmation keyboard; the actual broker call happens only
after the user taps "Confirm". Protected router (requires a linked account).
"""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app import emojis
from app.presenters import messages
from app.utils.telegram import safe_edit_text
from app.keyboards import inline
from app.services.broker_client import BrokerClientUser

router = Router(name="commands")


@router.message(Command("flat"))
async def cmd_flat(message: Message, account: dict[str, Any]) -> None:
  await message.answer(
    f"{emojis.WARNING} Confirm <b>FLAT</b> (close all positions) for account "
    f"<code>{account.get('account_id')}</code>?",
    reply_markup=inline.confirm_keyboard("flat"),
  )


@router.message(Command("prevent"))
async def cmd_prevent(message: Message, account: dict[str, Any]) -> None:
  await message.answer(
    f"{emojis.WARNING} Confirm <b>BLOCK</b> new orders for account "
    f"<code>{account.get('account_id')}</code>?",
    reply_markup=inline.confirm_keyboard("prevent"),
  )


@router.message(Command("allow"))
async def cmd_allow(message: Message, account: dict[str, Any]) -> None:
  await message.answer(
    f"Confirm <b>ALLOW</b> new orders for account "
    f"<code>{account.get('account_id')}</code>?",
    reply_markup=inline.confirm_keyboard("allow"),
  )


@router.callback_query(F.data.in_({"flat:confirm", "prevent:confirm", "allow:confirm"}))
async def cb_confirm(call: CallbackQuery, broker: BrokerClientUser) -> None:
  action = call.data.split(":", 1)[0]
  tg_id = call.from_user.id

  if action == "flat":
    result = await broker.flat(tg_id)
  elif action == "prevent":
    result = await broker.prevent(tg_id, enabled=True)
  else:  # allow
    result = await broker.prevent(tg_id, enabled=False)

  if result is None:
    await safe_edit_text(
      call.message, f"{emojis.CROSS} Command failed. Try again later."
    )
  else:
    await safe_edit_text(call.message, messages.format_command_result(result))
  await call.answer()


@router.callback_query(F.data.in_({"flat:cancel", "prevent:cancel", "allow:cancel"}))
async def cb_cancel(call: CallbackQuery) -> None:
  await safe_edit_text(call.message, "Cancelled.")
  await call.answer()
