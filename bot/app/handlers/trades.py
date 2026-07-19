"""
app/handlers/trades.py — /trades with inline pagination (protected router).
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app import emojis
from app.config import settings
from app.presenters import messages
from app.utils.telegram import safe_edit_text
from app.utils.timezone import offset_hours_from_payload
from app.keyboards import inline
from app.services.broker_client import BrokerClientAdmin, BrokerClientUser

router = Router(name="trades")

PAGE_SIZE = settings.BOT_VIEW_TRADES_PER_PAGE


@router.message(Command("trades"))
async def cmd_trades(
  message: Message, broker: BrokerClientUser, broker_admin: BrokerClientAdmin
) -> None:
  payload = await broker.list_trades(message.from_user.id, limit=PAGE_SIZE, offset=0)
  if payload is None:
    await message.answer(
      f"{emojis.WARNING} Failed to fetch trade data. Try again later."
    )
    return
  tz_offset = offset_hours_from_payload(await broker_admin.get_notification_timezone())
  await message.answer(
    messages.UserMessages.format_trades(payload, tz_offset),
    reply_markup=inline.trades_pagination(payload.get("page", {})),
  )


@router.callback_query(F.data.startswith("trades:"))
async def cb_trades_page(
  call: CallbackQuery, broker: BrokerClientUser, broker_admin: BrokerClientAdmin
) -> None:
  try:
    offset = int(call.data.split(":", 1)[1])
  except (IndexError, ValueError):
    await call.answer()
    return

  payload = await broker.list_trades(call.from_user.id, limit=PAGE_SIZE, offset=offset)
  if payload is None:
    await call.answer("Failed to load data", show_alert=True)
    return

  tz_offset = offset_hours_from_payload(await broker_admin.get_notification_timezone())
  await safe_edit_text(
    call.message,
    messages.UserMessages.format_trades(payload, tz_offset),
    reply_markup=inline.trades_pagination(payload.get("page", {})),
  )
  await call.answer()
