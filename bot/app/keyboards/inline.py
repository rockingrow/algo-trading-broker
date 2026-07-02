"""
app/keyboards/inline.py — Inline keyboard builders.

Callback data convention: ``"<action>:<arg>"`` (e.g. ``"flat:confirm"``,
``"trades:10"``). Kept well under Telegram's 64-byte callback-data limit.
"""

from __future__ import annotations

from typing import Any, Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.formatters.messages import SETTING_META


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


# ── Admin keyboards ─────────────────────────────────────────────────


def accounts_picker(
  accounts: list[dict[str, Any]], action_prefix: str
) -> InlineKeyboardMarkup:
  """One button per account → callback ``{action_prefix}:{account_id}``."""
  rows = [
    [
      InlineKeyboardButton(
        text=f"{a.get('account_name') or a.get('account_id')} · {a.get('account_id')}",
        callback_data=f"{action_prefix}:{a.get('account_id')}",
      )
    ]
    for a in accounts
  ]
  return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_trades_pagination(
  account_id: str, page: dict
) -> Optional[InlineKeyboardMarkup]:
  """Prev/Next for admin trade browsing → callback ``atr:{account_id}:{offset}``."""
  total = int(page.get("total", 0))
  limit = int(page.get("limit", 0)) or 1
  offset = int(page.get("offset", 0))

  row: list[InlineKeyboardButton] = []
  if offset > 0:
    row.append(
      InlineKeyboardButton(
        text="⬅️ Trước", callback_data=f"atr:{account_id}:{max(0, offset - limit)}"
      )
    )
  if offset + limit < total:
    row.append(
      InlineKeyboardButton(
        text="Sau ➡️", callback_data=f"atr:{account_id}:{offset + limit}"
      )
    )
  if not row:
    return None
  return InlineKeyboardMarkup(inline_keyboard=[row])


def settings_keyboard(states: list[dict[str, Any]]) -> InlineKeyboardMarkup:
  """A toggle button per setting → callback ``aset:{slug}``."""
  rows = []
  for s in states:
    key = str(s.get("setting"))
    label, slug = SETTING_META.get(key, (key, key))
    on = str(s.get("state")) == "ENABLED"
    rows.append(
      [
        InlineKeyboardButton(
          text=f"{'🟢' if on else '⚪️'} {label}",
          callback_data=f"aset:{slug}",
        )
      ]
    )
  return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_confirm(action: str, arg: str) -> InlineKeyboardMarkup:
  """Confirm/Cancel → callback ``{action}:{arg}:ok`` / ``{action}:{arg}:no``."""
  return InlineKeyboardMarkup(
    inline_keyboard=[
      [
        InlineKeyboardButton(text="✅ Xác nhận", callback_data=f"{action}:{arg}:ok"),
        InlineKeyboardButton(text="✖️ Hủy", callback_data=f"{action}:{arg}:no"),
      ]
    ]
  )
