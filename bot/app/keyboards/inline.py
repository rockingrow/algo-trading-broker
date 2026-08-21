"""
app/keyboards/inline.py — Inline keyboard builders.

Callback data convention: ``"<action>:<arg>"`` (e.g. ``"flat:confirm"``,
``"trades:10"``). Kept well under Telegram's 64-byte callback-data limit.
"""

from __future__ import annotations

from typing import Any, Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app import emojis
from app.constants import (
  CB_TRADE_DETAIL,
  CB_TRADE_EXIT,
  CB_TRADE_EXIT_CANCEL,
  CB_TRADE_EXIT_CONFIRM,
  CB_TRADE_SUMMARY,
  GATEWAYS_BY_MARKET,
  MARKETS,
)
from app.presenters.messages import AdminMessages
from app.utils.pagination import build_pagination_keyboard, build_pagination_row


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


def linked_accounts_picker(
  accounts: list[dict[str, Any]], page: dict
) -> InlineKeyboardMarkup:
  """One button per account linked to the caller → callback ``swacc:{id}``
  (the account's row id — a UUID, well under the 64-byte callback-data limit,
  so unlike ``aflat_candidates_picker`` no FSM-index indirection is needed).
  The active account is marked with a star.

  *accounts* is one page of the list; a Prev/Next row (``swpg:{offset}``) sits
  below the buttons so the picker pages in step with the table above it."""
  rows = [
    [
      InlineKeyboardButton(
        text=f"{emojis.STAR + ' ' if a.get('is_active') else ''}"
        f"{a.get('market')}-{a.get('gateway') or '?'}-{a.get('account_id')}",
        callback_data=f"swacc:{a.get('id')}",
      )
    ]
    for a in accounts
  ]
  nav = build_pagination_row(page, lambda offset: f"swpg:{offset}")
  if nav:
    rows.append(nav)
  return InlineKeyboardMarkup(inline_keyboard=rows)


def trades_pagination(page: dict) -> Optional[InlineKeyboardMarkup]:
  """Prev/Next buttons derived from the trades page metadata, or None when a
  single page covers everything."""
  return build_pagination_keyboard(page, lambda offset: f"trades:{offset}")


def accounts_pagination(page: dict) -> Optional[InlineKeyboardMarkup]:
  """Prev/Next for /myaccounts → callback ``myacc:{offset}``."""
  return build_pagination_keyboard(page, lambda offset: f"myacc:{offset}")


# ── Live trade card ─────────────────────────────────────────────────
# The broker builds the same two keyboards as raw Bot API dicts when it posts
# and refreshes a card (``broker/helpers/trade_card.py``); these rebuild them
# with aiogram types when the bot re-renders the card after a button tap.


def trade_card(trade_id: str, *, detailed: bool, closed: bool) -> Optional[InlineKeyboardMarkup]:
  """Detail/Summary + Exit for a live card, or None once the trade is over.

  Returning None is what strips the buttons: aiogram sends no ``reply_markup``,
  and the Bot API drops a message's keyboard when the field is absent."""
  if closed:
    return None
  toggle = (
    InlineKeyboardButton(
      text=f"{emojis.COLLAPSE} Summary", callback_data=f"{CB_TRADE_SUMMARY}:{trade_id}"
    )
    if detailed
    else InlineKeyboardButton(
      text=f"{emojis.DETAIL} Detail", callback_data=f"{CB_TRADE_DETAIL}:{trade_id}"
    )
  )
  return InlineKeyboardMarkup(
    inline_keyboard=[
      [
        toggle,
        InlineKeyboardButton(
          text=f"{emojis.CYCLE_CLOSED} Close", callback_data=f"{CB_TRADE_EXIT}:{trade_id}"
        ),
      ]
    ]
  )


def trade_exit_confirm(trade_id: str) -> InlineKeyboardMarkup:
  """Confirm/Cancel pair shown in place of a card's buttons before closing."""
  return InlineKeyboardMarkup(
    inline_keyboard=[
      [
        InlineKeyboardButton(
          text=f"{emojis.CHECK} Close it",
          callback_data=f"{CB_TRADE_EXIT_CONFIRM}:{trade_id}",
        ),
        InlineKeyboardButton(
          text=f"{emojis.CANCEL} Cancel",
          callback_data=f"{CB_TRADE_EXIT_CANCEL}:{trade_id}",
        ),
      ]
    ]
  )


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


def accounts_uuid_picker(
  accounts: list[dict[str, Any]], action_prefix: str
) -> InlineKeyboardMarkup:
  """One button per account → callback ``{action_prefix}:{account_uuid}``.

  Uses the account's row UUID (``id``) rather than the bare ``account_id`` so
  the target is unambiguous when an id is reused across gateways — used by the
  admin link-account flow. The UUID (36 chars) plus a short prefix stays well
  under Telegram's 64-byte callback-data limit."""
  rows = [
    [
      InlineKeyboardButton(
        text=f"{a.get('market')}/{a.get('gateway') or '?'} · "
        f"{a.get('account_name') or a.get('account_id')} · {a.get('account_id')}",
        callback_data=f"{action_prefix}:{a.get('id')}",
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
        text=f"{a.get('market')}/{a.get('gateway')} · "
        f"{a.get('account_name') or a.get('account_id')}",
        callback_data=f"aflatc:{i}",
      )
    ]
    for i, a in enumerate(accounts)
  ]
  return InlineKeyboardMarkup(inline_keyboard=rows)


# "All" sentinel travelling in aflm:/aflg:/afls: callbacks. One byte, keeps
# well clear of Telegram's 64-byte cap on callback_data, and out of the market
# / gateway namespaces (they only use A-Z uppercase names).
AFLAT_ALL = "a"


def aflat_strategy_picker(strategies: list[str]) -> InlineKeyboardMarkup:
  """One button per known strategy (plus an "All" row at the top) → callback
  ``afls:{index}`` where ``index`` addresses the strategies list held in FSM
  data. Strategy names are user-supplied and up to 50 chars — dropping them
  into callback_data risks the 64-byte limit and echoes user text back
  through Telegram, so the index indirection stays for them too."""
  rows: list[list[InlineKeyboardButton]] = [
    [InlineKeyboardButton(text="All strategies", callback_data=f"afls:{AFLAT_ALL}")]
  ]
  for i, name in enumerate(strategies):
    rows.append([InlineKeyboardButton(text=name, callback_data=f"afls:{i}")])
  return InlineKeyboardMarkup(inline_keyboard=rows)


def aflat_market_picker() -> InlineKeyboardMarkup:
  """One button per market (plus an "All" row) → callback ``aflm:{market}``
  or ``aflm:a`` for All."""
  rows: list[list[InlineKeyboardButton]] = [
    [InlineKeyboardButton(text="All markets", callback_data=f"aflm:{AFLAT_ALL}")]
  ]
  for market in MARKETS:
    rows.append([InlineKeyboardButton(text=market, callback_data=f"aflm:{market}")])
  return InlineKeyboardMarkup(inline_keyboard=rows)


def aflat_gateway_picker(market: Optional[str]) -> InlineKeyboardMarkup:
  """One button per gateway valid for *market* (plus an "All" row) → callback
  ``aflg:{gateway}`` or ``aflg:a`` for All. When *market* is None (the user
  picked "All markets") the union of every configured gateway is offered so
  the admin can still narrow scope."""
  if market is None:
    gateways: list[str] = []
    for lst in GATEWAYS_BY_MARKET.values():
      for gw in lst:
        if gw not in gateways:
          gateways.append(gw)
  else:
    gateways = list(GATEWAYS_BY_MARKET.get(market, []))

  rows: list[list[InlineKeyboardButton]] = [
    [InlineKeyboardButton(text="All gateways", callback_data=f"aflg:{AFLAT_ALL}")]
  ]
  for gw in gateways:
    rows.append([InlineKeyboardButton(text=gw, callback_data=f"aflg:{gw}")])
  return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_accounts_pagination(page: dict) -> Optional[InlineKeyboardMarkup]:
  """Prev/Next for /admin_accounts → callback ``aacc:{offset}``."""
  return build_pagination_keyboard(page, lambda offset: f"aacc:{offset}")


def admin_trades_pagination(
  account_id: str, page: dict
) -> Optional[InlineKeyboardMarkup]:
  """Prev/Next for admin trade browsing → callback ``atr:{account_id}:{offset}``."""
  return build_pagination_keyboard(page, lambda offset: f"atr:{account_id}:{offset}")


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
      [InlineKeyboardButton(text=gateway, callback_data=f"nacc:g:{market}:{gateway}")]
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
