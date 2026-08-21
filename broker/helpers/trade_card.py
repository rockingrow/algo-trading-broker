"""
broker/helpers/trade_card.py — The live trade card: body, buttons, callback data.

A *card* is the single Telegram message a subscribed owner gets for one trade.
It is posted when the trade first shows up on the TRADE subject and then
**edited in place** on every status change, so a user's chat holds one message
per trade rather than a stream of them. While the trade is running the card
carries a Detail / Close button row; once it reaches a terminal status the row
is dropped, because there is nothing left to act on.

This module is deliberately pure — no I/O, no ORM writes — so both the sender
(``TradeCardService``) and the tests can render a card without a database.

Two consumers must agree on the ``CALLBACK_*`` prefixes below:

* the **broker** builds the inline keyboard here and ships it with the message,
* the **bot service** (``bot/app/handlers/trade_card.py``) receives the taps,
  because the card is sent with ``BOT_TELEGRAM_TOKEN`` — the same bot the user
  started, and the one already long-polling for callback queries.

Keep the two in sync; the bot mirrors these constants with a comment pointing
back here. Every callback value is ``"<prefix>:<trade uuid>"`` — at most 42
bytes, comfortably inside Telegram's 64-byte callback-data limit.
"""

from __future__ import annotations

import html
import uuid
from typing import Any

from broker.db.models import Trade
from broker.helpers import emoji_constants as em
from broker.helpers.message_formatter import format_number
from broker.helpers.signal_helper import action_to_emoji
from broker.helpers.timezone_helper import format_notification_time
from broker.schemas.core import SignalActionEnum
from broker.schemas.trade_schema import TradeStatusEnum

# ── Callback data (mirrored by the bot) ──────────────────────────────
CALLBACK_DETAIL = "tc:d"  # expand the card
CALLBACK_SUMMARY = "tc:s"  # collapse it again
CALLBACK_EXIT = "tc:x"  # ask for confirmation
CALLBACK_EXIT_CONFIRM = "tc:xy"  # publish the close
CALLBACK_EXIT_CANCEL = "tc:xn"  # back to the normal card

# Statuses where the trade is over: the card freezes and loses its buttons.
TERMINAL_STATUSES = frozenset(
  {TradeStatusEnum.CLOSED, TradeStatusEnum.FLAT, TradeStatusEnum.REJECTED}
)

# Fallback icon for a last-action word that isn't itself a SignalActionEnum
# (REJECTED, TERMINAL_CLOSED, FORCED_CLOSED) — keyed by the trade's own status
# rather than the word, since all three are terminal in different ways.
_STATUS_EMOJI: dict[TradeStatusEnum, str] = {
  TradeStatusEnum.OPENED: em.TRADE_OPENED,
  TradeStatusEnum.PARTIALLY_CLOSED: em.TRADE_PARTIALLY_CLOSED,
  TradeStatusEnum.CLOSED: em.TRADE_CLOSED,
  TradeStatusEnum.FLAT: em.TRADE_FLAT,
  TradeStatusEnum.REJECTED: em.TRADE_REJECTED,
}

_DIVIDER = "-----------"

#: Same glyph the broadcast header shows for a closed cycle — reused so the
#: Close button reads as "this ends the trade" at a glance.
_CLOSE_ICON = em.CYCLE_CLOSED


def _esc(value: Any) -> str:
  """HTML-escape a value for a ``parse_mode=HTML`` message.

  Free-text columns (``comment``, ``reject_reason``) are written by the worker
  and a stray ``<`` in one of them would make Telegram reject the whole send,
  taking the card with it.
  """
  return html.escape(str(value)) if value is not None else "—"


def _enum_value(value: Any) -> str:
  """The wire value of an enum column, whether SQLAlchemy handed back the enum
  member or the raw string."""
  return str(getattr(value, "value", value))


def trade_status(trade: Trade) -> TradeStatusEnum:
  """The trade's status as an enum member, coercing the raw string a fake or a
  partially-loaded row might carry."""
  status = trade.status
  return status if isinstance(status, TradeStatusEnum) else TradeStatusEnum(str(status))


def is_terminal(trade: Trade) -> bool:
  """Whether this trade is over, i.e. the card should drop its buttons."""
  return trade_status(trade) in TERMINAL_STATUSES


def _action_icon(last_action: str, status: TradeStatusEnum) -> str:
  """Icon for one action word. ``last_action`` is usually a SignalActionEnum
  member (TP1/TP2/SL/R_SL/FLAT); REJECTED/TERMINAL_CLOSED/FORCED_CLOSED are
  not, so those fall back to the trade's own status dot."""
  try:
    return action_to_emoji(SignalActionEnum(last_action))
  except ValueError:
    return _STATUS_EMOJI.get(status, em.DEFAULT_SIGNAL)


def _last_action_block(trade: Trade, status: TradeStatusEnum) -> list[str]:
  """The boxed "Actions:" section, mirroring the broadcast message's own —
  the event that moved the trade, when it says something the entry action and
  bracket status do not.

  TP2, SL, R_SL, TERMINAL_CLOSED and FORCED_CLOSED all persist as ``CLOSED``,
  and ``action`` keeps the entry direction, so without this the card never
  says *how* a trade ended. Empty when there is nothing to add yet — a fresh
  OPENED trade, or a row that predates the ``last_action`` column.
  """
  last_action = trade.last_action
  if not last_action or last_action in ("OPENED", _enum_value(trade.action)):
    return []
  return [
    "",
    "Actions:",
    _DIVIDER,
    f"{_action_icon(last_action, status)} {_esc(last_action)}",
    _DIVIDER,
  ]


def _pnl(trade: Trade) -> float | None:
  """Realised PnL against the balance recorded when the trade opened, or None
  when either side of the subtraction is unknown."""
  if trade.account_balance is None or trade.account_balance_init is None:
    return None
  return float(trade.account_balance) - float(trade.account_balance_init)


def format_trade_card(
  trade: Trade,
  *,
  timezone_offset: str | None = None,
  detailed: bool = False,
  footer: str | None = None,
) -> str:
  """Render the card body for *trade* (Telegram HTML), styled like the public
  broadcast message: a ``[STATUS]`` header, a boxed entry block, and a boxed
  "Actions:" section for whatever moved the trade since it opened.

  The summary view carries what an owner glances at — direction, status,
  price, size, the stop/target levels and the running PnL. ``detailed=True``
  adds the bookkeeping an owner only wants on request: which strategy and
  account it came from, the broker's reference id, leverage and the worker's
  own comment or reject reason.

  *footer* appends one extra line, used to say an exit has been requested but
  the worker has not reported the close yet.
  """
  status = trade_status(trade)
  action = _enum_value(trade.action)
  terminal = status in TERMINAL_STATUSES
  status_icon = _CLOSE_ICON if terminal else em.CYCLE_RUNNING
  status_word = "CLOSED" if terminal else "RUNNING"
  price_label = "Close price" if terminal else "Price"

  lines = [
    f"[{status_icon}{status_word}]",
    f"{action_to_emoji(trade.action)} <b>{_esc(action)}</b> <b>{_esc(trade.symbol)}</b>",
    _DIVIDER,
    f"{price_label}: <code>{format_number(trade.price)}</code>",
  ]

  qty_risk = [f"Quantity: <code>{format_number(trade.quantity)}</code>"]
  if trade.risk_percent is not None:
    qty_risk.append(f"Risk: <code>{format_number(trade.risk_percent)}%</code>")
  lines.append(" | ".join(qty_risk))

  levels = [
    f"SL: <code>{format_number(trade.sl)}</code>",
    f"TP1: <code>{format_number(trade.tp1)}</code>",
    f"TP2: <code>{format_number(trade.tp2)}</code>",
  ]
  lines.append(" | ".join(levels))
  lines.append(_DIVIDER)

  if trade.account_balance is not None:
    lines.append(f"Balance: <b>{format_number(trade.account_balance)}</b>")
  pnl = _pnl(trade)
  if pnl is not None:
    sign = "+" if pnl >= 0 else ""
    lines.append(f"PnL: <b>{sign}{pnl:.2f}</b>")

  if detailed:
    lines.append("")
    lines.append(f"Strategy: <b>{_esc(trade.strategy)}</b>")
    lines.append(f"Account: <code>{_esc(trade.account_id)}</code>")
    lines.append(
      f"Market: <b>{_esc(_enum_value(trade.market))}</b> / <b>{_esc(trade.gateway)}</b>"
    )
    if trade.account_leverage is not None:
      lines.append(f"Leverage: <b>{_esc(trade.account_leverage)}</b>")
    if trade.ref_id:
      lines.append(f"Ref: <code>{_esc(trade.ref_id)}</code>")
    if trade.comment:
      lines.append(f"Comment: {_esc(trade.comment)}")
    if trade.reject_reason:
      lines.append(f"Reject reason: <b>{_esc(trade.reject_reason)}</b>")
    lines.append(f"Opened: {format_notification_time(trade.createdAt, timezone_offset)}")

  lines.append(f"Updated: {format_notification_time(trade.updatedAt, timezone_offset)}")
  lines.extend(_last_action_block(trade, status))

  if footer:
    lines.append("")
    lines.append(footer)

  return "\n".join(lines)


def _button(text: str, prefix: str, trade_id: uuid.UUID) -> dict[str, str]:
  return {"text": text, "callback_data": f"{prefix}:{trade_id}"}


def trade_card_keyboard(
  trade: Trade, *, detailed: bool = False
) -> dict[str, Any] | None:
  """The card's inline keyboard as a Bot API ``reply_markup`` dict, or None
  when the trade is over.

  None is what removes the buttons: ``edit_message`` omits the field entirely
  when the markup is None, and the Bot API drops a message's keyboard when the
  field is absent. So a card that reaches CLOSED / FLAT / REJECTED is left as a
  plain, final record of the trade.
  """
  if is_terminal(trade):
    return None
  toggle = (
    _button(f"{em.COLLAPSE} Summary", CALLBACK_SUMMARY, trade.id)
    if detailed
    else _button(f"{em.DETAIL} Detail", CALLBACK_DETAIL, trade.id)
  )
  return {"inline_keyboard": [[toggle, _button(f"{_CLOSE_ICON} Close", CALLBACK_EXIT, trade.id)]]}
