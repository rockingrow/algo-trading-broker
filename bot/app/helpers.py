"""
app/helpers.py — Small shared helpers for handlers.
"""

from __future__ import annotations

from typing import Optional

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, Message


async def safe_edit_text(
  message: Message,
  text: str,
  reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
  """Edit a message, swallowing the harmless "message is not modified" error
  Telegram raises when the new content/markup is identical (e.g. a double tap)."""
  try:
    await message.edit_text(text, reply_markup=reply_markup)
  except TelegramBadRequest as exc:
    if "message is not modified" not in str(exc).lower():
      raise
