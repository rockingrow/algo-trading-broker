"""
app/utils/pagination.py — Shared paging: page metadata + Prev/Next buttons.

Every table the bot renders pages through this module, so they all speak one
``page`` shape: ``{"total", "limit", "offset"}``. The broker already returns
that for trades; account lists come back whole, so ``paginate`` slices them
here and synthesises the same dict. Listings then differ only in the
callback-data prefix they page through.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Sequence, TypeVar

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app import emojis

T = TypeVar("T")


def paginate(
  items: Sequence[T], limit: int, offset: int
) -> tuple[list[T], dict[str, int]]:
  """Slice *items* into one page, plus the page metadata describing it.

  For endpoints the broker doesn't paginate (the account lists), so the
  presenters and keyboards can't tell the difference between a page it sliced
  and one the broker returned.

  An out-of-range offset clamps to the first page rather than rendering an
  empty table: a Prev/Next button on a message left open while the underlying
  list shrank shouldn't dead-end the user.
  """
  limit = max(1, limit)
  total = len(items)
  if offset < 0 or offset >= total:
    offset = 0
  return list(items[offset : offset + limit]), {
    "total": total,
    "limit": limit,
    "offset": offset,
  }


def build_pagination_row(
  page: dict[str, Any], callback_for: Callable[[int], str]
) -> list[InlineKeyboardButton]:
  """Prev/Next buttons derived from page metadata (``total``/``limit``/``offset``),
  or an empty row when a single page covers everything. ``callback_for`` maps a
  target offset to its callback-data string.

  Returned as a bare row so a keyboard that already has rows of its own (an
  account picker, say) can append the nav below them.
  """
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
  return row


def build_pagination_keyboard(
  page: dict[str, Any], callback_for: Callable[[int], str]
) -> Optional[InlineKeyboardMarkup]:
  """A keyboard holding nothing but the Prev/Next row, or None when a single
  page covers everything."""
  row = build_pagination_row(page, callback_for)
  return InlineKeyboardMarkup(inline_keyboard=[row]) if row else None
