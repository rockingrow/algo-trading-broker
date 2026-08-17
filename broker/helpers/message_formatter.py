"""
broker/helpers/message_formatter.py — Builds every Telegram message body in
one place: the signal-cycle broadcast, the completed-trade owner DM, and the
blocked-signal warning.

The signal bodies are cycle-shaped, not signal-shaped: a trade owns a single
message that is re-rendered from its stored event history each time a new
action arrives (see ``broker/services/broadcast_service.py``), so there is no
"format one signal" entry point any more.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation

from broker.constants import SIGNAL_BLOCKED
from broker.helpers import emoji_constants as em
from broker.helpers.signal_helper import action_to_emoji
from broker.helpers.timeframe_helper import format_timeframe
from broker.helpers.timezone_helper import format_notification_time
from broker.schemas.core import BroadcastStatusEnum, SignalActionEnum
from broker.schemas.webhook_schema import WebhookPayload


def _num(value) -> str:
  """Render a number the way a human writes it.

  ``str()`` on a DB ``Numeric`` (a ``Decimal``) leaks the column's scale: a
  zero comes out as ``0E-8`` and 0.104 as ``0.10400000``. Normalising drops
  the trailing zeros, and the exponent guard turns the scientific forms
  Decimal falls back to for zero and for large integers back into plain
  digits. Plain ``int``/``float`` values (JSONB event payloads, webhook
  prices) go through the same path so ``2340.0`` renders as ``2340``.
  """
  if value is None:
    return "—"
  if isinstance(value, (int, float)) and not isinstance(value, bool):
    try:
      value = Decimal(str(value))
    except InvalidOperation:
      return str(value)
  if not isinstance(value, Decimal):
    return str(value)
  normalised = value.normalize()
  if normalised.as_tuple().exponent > 0:
    normalised = normalised.quantize(Decimal(1))
  return f"{normalised:f}"


def format_completed_trade_message(
  trade,
  *,
  last_action: str | None = None,
  timezone_offset: str | None = None,
) -> str:
  """Telegram DM body sent to an account owner when one of their trades closes.

  Renders the persisted ``trades`` row (see ``broker.db.models.Trade``) — not a
  webhook payload — because this fires off the worker's TRADE completion event,
  after the trade has been upserted. Shows realised PnL when both the initial
  and current account balance are known.

  *last_action* is the event that ended the trade (``TP2``, ``SL``, ``R_SL``,
  ``FLAT``, ...), shown in brackets after the status: several events map onto
  the same ``CLOSED``, and the row itself keeps the entry action, so the status
  alone never says *how* the trade ended. Comes from the caller because only
  the TRADE event carries it.
  """
  status = getattr(trade.status, "value", str(trade.status))
  action = getattr(trade.action, "value", str(trade.action))
  # A FLATTED event yields status FLAT and last action FLAT — say it once.
  if last_action and last_action != status:
    status = f"{status} ({last_action})"

  lines = [
    f"{em.FLAT} <b>Trade completed</b>",
    f"Account: <code>{trade.account_id}</code>",
    f"Gateway: <b>{trade.gateway}</b>",
    f"Symbol: <b>{trade.symbol}</b>",
    f"Action: <b>{action}</b>",
    f"Status: <b>{status}</b>",
    f"Close price: <code>{_num(trade.price)}</code>",
    f"Quantity: <code>{_num(trade.quantity)}</code>",
  ]
  if trade.account_balance is not None:
    lines.append(f"Balance: <b>{_num(trade.account_balance)}</b>")
  if trade.account_balance is not None and trade.account_balance_init is not None:
    pnl = float(trade.account_balance) - float(trade.account_balance_init)
    sign = "+" if pnl >= 0 else ""
    lines.append(f"PnL: <b>{sign}{pnl:.2f}</b>")
  lines.append(f"Time: {format_notification_time(trade.updatedAt, timezone_offset)}")
  return "\n".join(lines)


# ── Broadcast cycle message ────────────────────────────────────────────────
#
# One Telegram message holds a whole signal cycle and is rewritten in place as
# the cycle progresses, so this renders the *entire* history from the stored
# events every time rather than formatting a single signal.

_STATUS_ICONS = {
  BroadcastStatusEnum.RUNNING: em.CYCLE_RUNNING,
  BroadcastStatusEnum.CLOSED: em.CYCLE_CLOSED,
}

_BROADCAST_DIVIDER = "-----------"

#: Actions that open a position, and are therefore the only ones whose SL/TP
#: levels are worth rendering.
_ENTRY_ACTIONS = frozenset({SignalActionEnum.LONG, SignalActionEnum.SHORT})


def _enum_value(value) -> str:
  """Bare string of an enum member, an enum's value, or a plain string."""
  return getattr(value, "value", str(value))


def _event_action(event: dict) -> SignalActionEnum | None:
  try:
    return SignalActionEnum(_enum_value(event.get("action")))
  except ValueError:
    return None


def _event_time(value, timezone_offset: str | None) -> str:
  """Render an event's stored timestamp (ISO text in JSONB) for display."""
  if value is None:
    return ""
  if isinstance(value, datetime):
    return format_notification_time(value, timezone_offset)
  try:
    return format_notification_time(datetime.fromisoformat(str(value)), timezone_offset)
  except ValueError:
    return str(value)


def _broadcast_flags_line(event: dict) -> str:
  """Compact one-line rendering of the entry's optional position flags.

  Only flags the strategy actually sent are shown, so a minimal webhook stays
  minimal in the channel.
  """

  def _flag(value: bool) -> str:
    return em.FLAG_ON if value else em.FLAG_OFF

  parts: list[str] = []
  if event.get("tp1_percent") is not None:
    parts.append(f"TP1%: {_num(event.get('tp1_percent'))}%")
  if event.get("move_sl_to_be") is not None:
    parts.append(f"SL→BE: {_flag(bool(event.get('move_sl_to_be')))}")
  if event.get("is_running") is not None:
    parts.append(f"Running: {_flag(bool(event.get('is_running')))}")
  if event.get("is_scale_position") is not None:
    scale = f"Scale: {_flag(bool(event.get('is_scale_position')))}"
    if event.get("scale_strategy"):
      scale += f" {event.get('scale_strategy')}"
    parts.append(scale)
  return " | ".join(parts)


def _broadcast_event_block(event: dict, *, timezone_offset: str | None) -> str:
  """One entry of the cycle timeline: what happened, at what price, when."""
  action = _event_action(event)
  label = _enum_value(event.get("action"))
  icon = action_to_emoji(action) if action is not None else em.DEFAULT_SIGNAL

  head = f"{icon} <b>{label}</b>"
  if event.get("price") is not None:
    head += f" @ <code>{_num(event.get('price'))}</code>"
  if event.get("quantity") is not None:
    head += f" × <code>{_num(event.get('quantity'))}</code>"
  if event.get("risk_percent") is not None:
    head += f" | Risk: <code>{_num(event.get('risk_percent'))}%</code>"

  lines = [head]

  # Levels and position flags belong to the entry that set them; repeating them
  # under every TP/SL line would just be noise.
  if action in _ENTRY_ACTIONS:
    levels = [
      f"{name}: <code>{_num(event.get(key))}</code>"
      for name, key in (("SL", "sl"), ("TP1", "tp1"), ("TP2", "tp2"))
      if event.get(key) is not None
    ]
    if levels:
      lines.append(" | ".join(levels))
    flags = _broadcast_flags_line(event)
    if flags:
      lines.append(flags)

  stamp = _event_time(event.get("timestamp"), timezone_offset)
  attempt = event.get("attempt")
  if stamp:
    lines.append(
      f"<i>{stamp}</i>" + (f" {em.CYCLE_RETRY} attempt {attempt}" if attempt else "")
    )
  return "\n".join(lines)


def worker_label(worker) -> str:
  """Short, public-safe name for a worker row: gateway + masked account id.

  The account id is masked to its last four characters on purpose — the public
  broadcast shows *that* a worker took the signal and where it stands, and the
  owner recognises their own line, without publishing anyone's full account
  number to a channel.
  """
  account_id = str(getattr(worker, "account_id", "") or "")
  masked = f"****{account_id[-4:]}" if len(account_id) > 4 else (account_id or "?")
  gateway = getattr(worker, "gateway", None)
  if not gateway:
    market = getattr(worker, "market", None)
    gateway = _enum_value(market) if market is not None else ""
  return f"{gateway} {masked}".strip()


def _format_worker_table(workers) -> str:
  """Two-column ``worker | latest status`` table of who executed the cycle.

  Rendered inside a ``<pre>`` block so Telegram's monospace font lines the
  columns up — the same table re-rendered on every update, which is what makes
  the message readable as it fills in.
  """
  rows = [(worker_label(w), _enum_value(w.latest_status)) for w in workers or []]
  if not rows:
    return ""
  width = max(len(name) for name, _ in rows)
  width = max(width, len("Worker"))
  header = f"{'Worker'.ljust(width)}  Status"
  lines = "\n".join(f"{name.ljust(width)}  {status}" for name, status in rows)
  return f"\nExecutions ({len(rows)})\n<pre>{header}\n{lines}</pre>"


def format_broadcast_message(
  record,
  *,
  workers=None,
  timezone_offset: str | None = None,
  include_raw: bool = False,
) -> str:
  """Telegram body for a whole signal cycle (``broadcast_messages`` row).

  *record* is duck-typed on the ORM model — ``symbol``, ``timeframe``,
  ``strategy``, ``signal_uxid``, ``status`` and the ``events`` list — so the
  service can render a row it just wrote without a round trip.

  The header carries the cycle's live state (⏳ running / 🏁 closed) and the
  body is the timeline of every signal received so far.

  *workers* (``broadcast_message_workers`` rows) appends the execution table —
  who took the signal and their latest status — which is what the **public**
  audience gets in place of the operator-facing raw dump. ``include_raw``
  mirrors the ``notification_include_signal_raw`` setting and appends the
  indicators / inputs of the most recent signal only, since the cycle would
  otherwise repeat a full raw dump per action.
  """
  events = [event for event in (record.events or []) if isinstance(event, dict)]
  status = record.status
  try:
    status = BroadcastStatusEnum(_enum_value(status))
  except ValueError:
    status = BroadcastStatusEnum.RUNNING

  entry_action = _event_action(events[0]) if events else None
  entry_icon = (
    action_to_emoji(entry_action) if entry_action is not None else em.DEFAULT_SIGNAL
  )
  timeframe = f" ({format_timeframe(record.timeframe)})" if record.timeframe else ""

  header = (
    f"{entry_icon} <b>{record.symbol}</b>{timeframe} "
    f"{_STATUS_ICONS.get(status, em.CYCLE_RUNNING)} <b>{status.value}</b>\n"
    f"Strategy: <b>{record.strategy}</b>\n"
    f"Signal: <code>{record.signal_uxid}</code>\n"
  )

  timeline = "\n".join(
    _broadcast_event_block(event, timezone_offset=timezone_offset) for event in events
  )

  raw = ""
  if include_raw and events:
    raw = _format_raw_dicts(events[-1].get("indicators"), events[-1].get("inputs"))

  executions = _format_worker_table(workers)

  return (
    f"{header}{_BROADCAST_DIVIDER}\n{timeline}\n{_BROADCAST_DIVIDER}{executions}{raw}"
  )


def _format_raw_dicts(indicators, inputs) -> str:
  """Indicators / inputs blocks for a broadcast, from plain dicts."""
  parts: list[str] = []
  for title, data in (("Indicators", indicators), ("Inputs", inputs)):
    if not isinstance(data, dict):
      continue
    values = {k: v for k, v in data.items() if v is not None}
    if values:
      lines = "\n".join(f"  {k}: {v}" for k, v in values.items())
      parts.append(f"{title}:\n{lines}")
  return ("\n" + "\n".join(parts)) if parts else ""


def format_blocked_message(payload: WebhookPayload) -> str:
  """Telegram body sent when signal processing is disabled."""
  return (
    f"{em.BLOCKED} <b>Broker signal blocked</b>\n"
    f"Symbol: <b>{payload.symbol}</b>\n"
    f"Reason: Signal processing is temporarily disabled "
    f"(<code>{SIGNAL_BLOCKED}</code> != 1)"
  )
