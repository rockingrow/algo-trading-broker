"""
app/handlers/trades.py — /trades with inline pagination (protected router).
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app import emojis
from app.config import settings
from app.formatters import messages
from app.helpers import safe_edit_text
from app.keyboards import inline
from app.services.broker_client import BrokerClient

router = Router(name="trades")

PAGE_SIZE = settings.BOT_VIEW_TRADES_PER_PAGE


@router.message(Command("trades"))
async def cmd_trades(message: Message, broker: BrokerClient) -> None:
  payload = await broker.list_trades(message.from_user.id, limit=PAGE_SIZE, offset=0)
  if payload is None:
    await message.answer(
      f"{emojis.WARNING} Failed to fetch trade data. Try again later."
    )
    return
  await message.answer(
    messages.format_trades(payload),
    reply_markup=inline.trades_pagination(payload.get("page", {})),
  )


@router.callback_query(F.data.startswith("trades:"))
async def cb_trades_page(call: CallbackQuery, broker: BrokerClient) -> None:
  try:
    offset = int(call.data.split(":", 1)[1])
  except (IndexError, ValueError):
    await call.answer()
    return

  payload = await broker.list_trades(call.from_user.id, limit=PAGE_SIZE, offset=offset)
  if payload is None:
    await call.answer("Failed to load data", show_alert=True)
    return

  await safe_edit_text(
    call.message,
    messages.format_trades(payload),
    reply_markup=inline.trades_pagination(payload.get("page", {})),
  )
  await call.answer()
