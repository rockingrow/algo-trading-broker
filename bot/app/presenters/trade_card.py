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

_STATUS_EMOJI: dict[str, str] = {
  "OPENED": emojis.TRADE_OPENED,
  "PARTIALLY_CLOSED": emojis.TRADE_PARTIALLY_CLOSED,
  "CLOSED": emojis.TRADE_CLOSED,
  "FLAT": emojis.TRADE_FLAT,
  "REJECTED": emojis.TRADE_REJECTED,
}

_STATUS_LABEL: dict[str, str] = {
  "OPENED": "Opened",
  "PARTIALLY_CLOSED": "Partially closed",
  "CLOSED": "Closed",
  "FLAT": "Flatted",
  "REJECTED": "Rejected",
}


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


def _last_action_suffix(trade: dict[str, Any], status: str) -> str:
  """`` (SL)`` — the event that put the trade in this status, when it says
  something the status does not. Mirrors the broker-side twin: TP2/SL/R_SL all
  persist as ``CLOSED``, so without it the card never says how a trade ended."""
  last_action = trade.get("last_action")
  if not last_action or last_action == status:
    return ""
  return f" ({_esc(last_action)})"


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
  """Render *trade* as the card body — see the broker-side twin for the shape."""
  status = str(trade.get("status"))
  dot = _STATUS_EMOJI.get(status, emojis.SATELLITE)
  label = _STATUS_LABEL.get(status, status)
  price_label = "Close price" if status in TERMINAL_TRADE_STATUSES else "Price"

  lines = [
    f"{dot} <b>{_esc(trade.get('symbol'))}</b> · <b>{_esc(trade.get('action'))}</b>",
    f"Status: <b>{label}</b>{_last_action_suffix(trade, status)}",
    f"{price_label}: <code>{_num(trade.get('price'))}</code>",
    f"Quantity: <code>{_num(trade.get('quantity'))}</code>",
    f"SL: <code>{_num(trade.get('sl'))}</code> | "
    f"TP1: <code>{_num(trade.get('tp1'))}</code> | "
    f"TP2: <code>{_num(trade.get('tp2'))}</code>",
  ]

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
    if trade.get("risk_percent") is not None:
      lines.append(f"Risk: <code>{_num(trade.get('risk_percent'))}%</code>")
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

  if footer:
    lines.append("")
    lines.append(footer)

  return "\n".join(lines)


def format_exit_prompt(trade: dict[str, Any]) -> str:
  """The confirmation the Exit button puts in place of the card.

  Replacing the whole body (rather than only swapping the keyboard) is what
  lets Cancel restore a known-good card without having to remember whether the
  user was looking at the summary or the detail view.
  """
  return (
    f"{emojis.WARNING} Close <b>{_esc(trade.get('symbol'))}</b> "
    f"(<b>{_esc(trade.get('action'))}</b>) now?\n\n"
    f"<i>This publishes a FLAT for strategy "
    f"<code>{_esc(trade.get('strategy'))}</code> on this symbol, so any other "
    f"position it holds on "
    f"<code>{_esc(trade.get('symbol'))}</code> closes too.</i>"
  )


EXIT_REQUESTED_FOOTER = (
  f"{emojis.PENDING} <i>Exit requested — waiting for the worker to close it. "
  "This card updates itself when it does.</i>"
)
