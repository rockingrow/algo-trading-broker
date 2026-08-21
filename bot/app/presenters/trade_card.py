"""
app/presenters/trade_card.py — Bot-side rendering of the live trade card.

The card itself is *posted* by the broker (``broker/helpers/trade_card.py``),
which renders it straight from the ``trades`` row. This module renders the same
card from the JSON the broker's ``GET /v1/telegram/{id}/trades/{trade_id}``
returns, so tapping Detail or Summary rewrites the message without it changing
character.

Keeping the two renderers in step matters more than sharing code would help:
they read different inputs (SQLAlchemy ``Decimal`` columns vs. JSON floats) and
live in separately deployed services. Any change to the card's shape belongs in
both files.
"""

from __future__ import annotations

import html
from typing import Any, Optional

from app import emojis
from app.constants import TERMINAL_TRADE_STATUSES
from app.utils.timezone import format_local_time

# Fallback icon for a last-action word that has no action icon of its own
# (REJECTED, TERMINAL_CLOSED, FORCED_CLOSED) — keyed by the trade's own status
# rather than the word, since all three are terminal in different ways.
_STATUS_EMOJI: dict[str, str] = {
  "OPENED": emojis.TRADE_OPENED,
  "PARTIALLY_CLOSED": emojis.TRADE_PARTIALLY_CLOSED,
  "CLOSED": emojis.TRADE_CLOSED,
  "FLAT": emojis.TRADE_FLAT,
  "REJECTED": emojis.TRADE_REJECTED,
}

# Mirrors broker/helpers/signal_helper.py's ``_ACTION_EMOJI``.
_ACTION_EMOJI: dict[str, str] = {
  "LONG": emojis.LONG,
  "SHORT": emojis.SHORT,
  "TP1": emojis.TP1,
  "TP2": emojis.TP2,
  "R_SL": emojis.R_SL,
  "SL": emojis.SL,
  "FLAT": emojis.FLAT,
}

_DIVIDER = "-----------"

#: Same glyph the broadcast header shows for a closed cycle — reused so the
#: Close button reads as "this ends the trade" at a glance.
_CLOSE_ICON = emojis.CYCLE_CLOSED


def _esc(value: Any) -> str:
  return html.escape(str(value)) if value is not None else "—"


def _num(value: Any) -> str:
  """Render a number the way the broker's ``format_decimal`` does.

  The broker normalises a ``Decimal`` to drop the column's scale; over JSON the
  same value arrives as a float whose ``str()`` keeps a trailing ``.0``. Fixing
  the precision and stripping the tail lands on the same text for every value a
  price or size realistically takes.
  """
  if value is None:
    return "—"
  try:
    number = float(value)
  except (TypeError, ValueError):
    return _esc(value)
  text = f"{number:.8f}".rstrip("0").rstrip(".")
  return text or "0"


def _action_icon(last_action: str, status: str) -> str:
  return _ACTION_EMOJI.get(last_action, _STATUS_EMOJI.get(status, emojis.SATELLITE))


def _last_action_block(trade: dict[str, Any], status: str) -> list[str]:
  """The boxed "Actions:" section, mirroring the broker-side twin and the
  broadcast message's own — the event that moved the trade, when it says
  something the entry action and bracket status do not. Empty when there is
  nothing to add yet — a fresh OPENED trade, or a row that predates the
  ``last_action`` field."""
  last_action = trade.get("last_action")
  if not last_action or last_action in ("OPENED", str(trade.get("action"))):
    return []
  return [
    "",
    "Actions:",
    _DIVIDER,
    f"{_action_icon(last_action, status)} {_esc(last_action)}",
    _DIVIDER,
  ]


def is_closed(trade: dict[str, Any]) -> bool:
  """Whether the trade is over, i.e. the card should carry no buttons."""
  return str(trade.get("status")) in TERMINAL_TRADE_STATUSES


def format_trade_card(
  trade: dict[str, Any],
  tz_offset_hours: float,
  *,
  detailed: bool = False,
  footer: Optional[str] = None,
) -> str:
  """Render *trade* as the card body — see the broker-side twin for the shape,
  styled like the public broadcast message: a ``[STATUS]`` header, a boxed
  entry block, and a boxed "Actions:" section for whatever moved the trade
  since it opened."""
  status = str(trade.get("status"))
  action = str(trade.get("action"))
  terminal = status in TERMINAL_TRADE_STATUSES
  status_icon = _CLOSE_ICON if terminal else emojis.CYCLE_RUNNING
  status_word = "CLOSED" if terminal else "RUNNING"
  price_label = "Close price" if terminal else "Price"

  lines = [
    f"[{status_icon}{status_word}]",
    f"{_ACTION_EMOJI.get(action, emojis.SATELLITE)} <b>{_esc(action)}</b> "
    f"<b>{_esc(trade.get('symbol'))}</b>",
    _DIVIDER,
    f"{price_label}: <code>{_num(trade.get('price'))}</code>",
  ]

  qty_risk = [f"Quantity: <code>{_num(trade.get('quantity'))}</code>"]
  if trade.get("risk_percent") is not None:
    qty_risk.append(f"Risk: <code>{_num(trade.get('risk_percent'))}%</code>")
  lines.append(" | ".join(qty_risk))

  lines.append(
    f"SL: <code>{_num(trade.get('sl'))}</code> | "
    f"TP1: <code>{_num(trade.get('tp1'))}</code> | "
    f"TP2: <code>{_num(trade.get('tp2'))}</code>"
  )
  lines.append(_DIVIDER)

  balance = trade.get("account_balance")
  if balance is not None:
    lines.append(f"Balance: <b>{_num(balance)}</b>")
  balance_init = trade.get("account_balance_init")
  if balance is not None and balance_init is not None:
    pnl = float(balance) - float(balance_init)
    sign = "+" if pnl >= 0 else ""
    lines.append(f"PnL: <b>{sign}{pnl:.2f}</b>")

  if detailed:
    lines.append("")
    lines.append(f"Strategy: <b>{_esc(trade.get('strategy'))}</b>")
    lines.append(f"Account: <code>{_esc(trade.get('account_id'))}</code>")
    lines.append(
      f"Market: <b>{_esc(trade.get('market'))}</b> / <b>{_esc(trade.get('gateway'))}</b>"
    )
    if trade.get("account_leverage") is not None:
      lines.append(f"Leverage: <b>{_esc(trade.get('account_leverage'))}</b>")
    if trade.get("ref_id"):
      lines.append(f"Ref: <code>{_esc(trade.get('ref_id'))}</code>")
    if trade.get("comment"):
      lines.append(f"Comment: {_esc(trade.get('comment'))}")
    if trade.get("reject_reason"):
      lines.append(f"Reject reason: <b>{_esc(trade.get('reject_reason'))}</b>")
    lines.append(
      f"Opened: {format_local_time(trade.get('createdAt'), tz_offset_hours)}"
    )

  lines.append(f"Updated: {format_local_time(trade.get('updatedAt'), tz_offset_hours)}")
  lines.extend(_last_action_block(trade, status))

  if footer:
    lines.append("")
    lines.append(footer)

  return "\n".join(lines)


def format_exit_prompt(trade: dict[str, Any]) -> str:
  """The confirmation the Close button puts in place of the card.

  Replacing the whole body (rather than only swapping the keyboard) is what
  lets Cancel restore a known-good card without having to remember whether the
  user was looking at the summary or the detail view.
  """
  return (
    f"{emojis.WARNING} Close <b>{_esc(trade.get('symbol'))}</b> "
    f"(<b>{_esc(trade.get('action'))}</b>) now?\n\n"
    f"<i>This closes exactly this trade. Any other positions for strategy "
    f"<code>{_esc(trade.get('strategy'))}</code> on "
    f"<code>{_esc(trade.get('symbol'))}</code> are not affected.</i>"
  )


EXIT_REQUESTED_FOOTER = (
  f"{emojis.PENDING} <i>Close requested — waiting for the worker to close it. "
  "This card updates itself when it does.</i>"
)
