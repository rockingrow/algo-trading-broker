"""
app/keyboards/inline.py — Inline keyboard builders.

Callback data convention: ``"<action>:<arg>"`` (e.g. ``"flat:confirm"``,
``"trades:10"``). Kept well under Telegram's 64-byte callback-data limit.
"""

from __future__ import annotations

from typing import Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def confirm_keyboard(action: str) -> InlineKeyboardMarkup:
  """Confirm/Cancel pair for a destructive action (flat / prevent / unlink)."""
  return InlineKeyboardMarkup(
    inline_keyboard=[
      [
        InlineKeyboardButton(text="✅ Xác nhận", callback_data=f"{action}:confirm"),
        InlineKeyboardButton(text="✖️ Hủy", callback_data=f"{action}:cancel"),
      ]
    ]
  )


def trades_pagination(page: dict) -> Optional[InlineKeyboardMarkup]:
  """Prev/Next buttons derived from the trades page metadata, or None when a
  single page covers everything."""
  total = int(page.get("total", 0))
  limit = int(page.get("limit", 0)) or 1
  offset = int(page.get("offset", 0))

  row: list[InlineKeyboardButton] = []
  if offset > 0:
    row.append(
      InlineKeyboardButton(
        text="⬅️ Trước", callback_data=f"trades:{max(0, offset - limit)}"
      )
    )
  if offset + limit < total:
    row.append(
      InlineKeyboardButton(text="Sau ➡️", callback_data=f"trades:{offset + limit}")
    )

  if not row:
    return None
  return InlineKeyboardMarkup(inline_keyboard=[row])
