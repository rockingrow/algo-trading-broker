"""
app/utils/pagination.py — Shared Prev/Next inline keyboard builder.

Used by both the user-facing and admin trade listings, which only differ in
the callback-data prefix they page through.
"""

from __future__ import annotations

from typing import Callable, Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app import emojis


def build_pagination_keyboard(
  page: dict, callback_for: Callable[[int], str]
) -> Optional[InlineKeyboardMarkup]:
  """Prev/Next buttons derived from page metadata (``total``/``limit``/``offset``),
  or None when a single page covers everything. ``callback_for`` maps a target
  offset to its callback-data string."""
  total = int(page.get("total", 0))
  limit = int(page.get("limit", 0)) or 1
  offset = int(page.get("offset", 0))

  row: list[InlineKeyboardButton] = []
  if offset > 0:
    row.append(
      InlineKeyboardButton(
        text=f"{emojis.ARROW_LEFT} Prev",
        callback_data=callback_for(max(0, offset - limit)),
      )
    )
  if offset + limit < total:
    row.append(
      InlineKeyboardButton(
        text=f"Next {emojis.ARROW_RIGHT}",
        callback_data=callback_for(offset + limit),
      )
    )

  if not row:
    return None
  return InlineKeyboardMarkup(inline_keyboard=[row])
