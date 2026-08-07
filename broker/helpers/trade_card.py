"""
broker/helpers/trade_card.py — The live trade card: body, buttons, callback data.

A *card* is the single Telegram message a subscribed owner gets for one trade.
It is posted when the trade first shows up on the TRADE subject and then
**edited in place** on every status change, so a user's chat holds one message
per trade rather than a stream of them. While the trade is running the card
carries a Detail / Exit button row; once it reaches a terminal status the row
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
from broker.helpers.timezone_helper import format_notification_time
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

_STATUS_EMOJI: dict[TradeStatusEnum, str] = {
  TradeStatusEnum.OPENED: em.TRADE_OPENED,
  TradeStatusEnum.PARTIALLY_CLOSED: em.TRADE_PARTIALLY_CLOSED,
  TradeStatusEnum.CLOSED: em.TRADE_CLOSED,
  TradeStatusEnum.FLAT: em.TRADE_FLAT,
  TradeStatusEnum.REJECTED: em.TRADE_REJECTED,
}

_STATUS_LABEL: dict[TradeStatusEnum, str] = {
  TradeStatusEnum.OPENED: "Opened",
  TradeStatusEnum.PARTIALLY_CLOSED: "Partially closed",
  TradeStatusEnum.CLOSED: "Closed",
  TradeStatusEnum.FLAT: "Flatted",
  TradeStatusEnum.REJECTED: "Rejected",
}


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


def _last_action_suffix(trade: Trade, status: TradeStatusEnum) -> str:
  """`` (SL)`` — the event that put the trade in this status, when it says
  something the status does not.

  TP2, SL, R_SL, TERMINAL_CLOSED and FORCED_CLOSED all persist as ``CLOSED``,
  and ``action`` keeps the entry direction, so without this the card never says
  *how* a trade ended. Skipped when the two carry the same word (a FLATTED
  event yields status FLAT and last action FLAT — say it once) and when the
  row predates the column.
  """
  last_action = trade.last_action
  if not last_action or last_action == status.value:
    return ""
  return f" ({_esc(last_action)})"


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
  """Render the card body for *trade* (Telegram HTML).

  The summary view carries what an owner glances at — direction, status,
  price, size, the stop/target levels and the running PnL. ``detailed=True``
  adds the bookkeeping an owner only wants on request: which strategy and
  account it came from, the broker's reference id, leverage, risk and the
  worker's own comment or reject reason.

  *footer* appends one extra line, used to say an exit has been requested but
  the worker has not reported the close yet.
  """
  status = trade_status(trade)
  action = _enum_value(trade.action)
  dot = _STATUS_EMOJI.get(status, em.DEFAULT_SIGNAL)
  label = _STATUS_LABEL.get(status, status.value)
  price_label = "Close price" if status in TERMINAL_STATUSES else "Price"

  lines = [
    f"{dot} <b>{_esc(trade.symbol)}</b> · <b>{_esc(action)}</b>",
    f"Status: <b>{label}</b>{_last_action_suffix(trade, status)}",
    f"{price_label}: <code>{format_number(trade.price)}</code>",
    f"Quantity: <code>{format_number(trade.quantity)}</code>",
  ]

  levels = [
    f"SL: <code>{format_number(trade.sl)}</code>",
    f"TP1: <code>{format_number(trade.tp1)}</code>",
    f"TP2: <code>{format_number(trade.tp2)}</code>",
  ]
  lines.append(" | ".join(levels))

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
    if trade.risk_percent is not None:
      lines.append(f"Risk: <code>{format_number(trade.risk_percent)}%</code>")
    if trade.ref_id:
      lines.append(f"Ref: <code>{_esc(trade.ref_id)}</code>")
    if trade.comment:
      lines.append(f"Comment: {_esc(trade.comment)}")
    if trade.reject_reason:
      lines.append(f"Reject reason: <b>{_esc(trade.reject_reason)}</b>")
    lines.append(f"Opened: {format_notification_time(trade.createdAt, timezone_offset)}")

  lines.append(f"Updated: {format_notification_time(trade.updatedAt, timezone_offset)}")

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
  return {"inline_keyboard": [[toggle, _button(f"{em.EXIT} Exit", CALLBACK_EXIT, trade.id)]]}
