"""
app/keyboards/inline.py — Inline keyboard builders.

Callback data convention: ``"<action>:<arg>"`` (e.g. ``"flat:confirm"``,
``"trades:10"``). Kept well under Telegram's 64-byte callback-data limit.
"""

from __future__ import annotations

from typing import Any, Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app import emojis
from app.constants import GATEWAYS_BY_MARKET, MARKETS
from app.presenters.messages import AdminMessages
from app.utils.pagination import build_pagination_keyboard


def confirm_keyboard(action: str) -> InlineKeyboardMarkup:
  """Confirm/Cancel pair for a destructive action (flat / prevent / unlink)."""
  return InlineKeyboardMarkup(
    inline_keyboard=[
      [
        InlineKeyboardButton(
          text=f"{emojis.CHECK} Confirm", callback_data=f"{action}:confirm"
        ),
        InlineKeyboardButton(
          text=f"{emojis.CANCEL} Cancel", callback_data=f"{action}:cancel"
        ),
      ]
    ]
  )


def linked_accounts_picker(accounts: list[dict[str, Any]]) -> InlineKeyboardMarkup:
  """One button per account linked to the caller → callback ``swacc:{id}``
  (the account's row id — a UUID, well under the 64-byte callback-data limit,
  so unlike ``aflat_candidates_picker`` no FSM-index indirection is needed).
  The active account is marked with a star."""
  rows = [
    [
      InlineKeyboardButton(
        text=f"{emojis.STAR + ' ' if a.get('is_active') else ''}"
        f"{a.get('market_type')}-{a.get('gateway') or '?'}-{a.get('account_id')}",
        callback_data=f"swacc:{a.get('id')}",
      )
    ]
    for a in accounts
  ]
  return InlineKeyboardMarkup(inline_keyboard=rows)


def trades_pagination(page: dict) -> Optional[InlineKeyboardMarkup]:
  """Prev/Next buttons derived from the trades page metadata, or None when a
  single page covers everything."""
  return build_pagination_keyboard(page, lambda offset: f"trades:{offset}")


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


def aflat_candidates_picker(accounts: list[dict[str, Any]]) -> InlineKeyboardMarkup:
  """One button per account sharing a colliding account_id → callback
  ``aflatc:{index}`` (the account itself is resolved from FSM data by index,
  not from callback_data — see admin.py's /aflat docstring)."""
  rows = [
    [
      InlineKeyboardButton(
        text=f"{a.get('market_type')}/{a.get('gateway')} · "
        f"{a.get('account_name') or a.get('account_id')}",
        callback_data=f"aflatc:{i}",
      )
    ]
    for i, a in enumerate(accounts)
  ]
  return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_trades_pagination(
  account_id: str, page: dict
) -> Optional[InlineKeyboardMarkup]:
  """Prev/Next for admin trade browsing → callback ``atr:{account_id}:{offset}``."""
  return build_pagination_keyboard(
    page, lambda offset: f"atr:{account_id}:{offset}"
  )


def settings_keyboard(states: list[dict[str, Any]]) -> InlineKeyboardMarkup:
  """A toggle button per setting → callback ``aset:{slug}``."""
  rows = []
  for s in states:
    key = str(s.get("setting"))
    label, slug = AdminMessages.SETTING_META.get(key, (key, key))
    on = str(s.get("state")) == "ENABLED"
    dot = emojis.GREEN_CIRCLE if on else emojis.WHITE_CIRCLE
    rows.append(
      [
        InlineKeyboardButton(
          text=f"{dot} {label}",
          callback_data=f"aset:{slug}",
        )
      ]
    )
  return InlineKeyboardMarkup(inline_keyboard=rows)


def market_picker() -> InlineKeyboardMarkup:
  """One button per market → callback ``nacc:m:{market}``."""
  return InlineKeyboardMarkup(
    inline_keyboard=[
      [
        InlineKeyboardButton(text=market, callback_data=f"nacc:m:{market}")
        for market in MARKETS
      ]
    ]
  )


def gateway_picker(market: str) -> InlineKeyboardMarkup:
  """One button per gateway valid for *market* → callback ``nacc:g:{market}:{gateway}``."""
  gateways = GATEWAYS_BY_MARKET.get(market, [])
  return InlineKeyboardMarkup(
    inline_keyboard=[
      [
        InlineKeyboardButton(
          text=gateway, callback_data=f"nacc:g:{market}:{gateway}"
        )
      ]
      for gateway in gateways
    ]
  )


def admin_confirm(action: str, arg: str) -> InlineKeyboardMarkup:
  """Confirm/Cancel → callback ``{action}:{arg}:ok`` / ``{action}:{arg}:no``."""
  return InlineKeyboardMarkup(
    inline_keyboard=[
      [
        InlineKeyboardButton(
          text=f"{emojis.CHECK} Confirm", callback_data=f"{action}:{arg}:ok"
        ),
        InlineKeyboardButton(
          text=f"{emojis.CANCEL} Cancel", callback_data=f"{action}:{arg}:no"
        ),
      ]
    ]
  )
