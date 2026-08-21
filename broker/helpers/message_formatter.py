"""
broker/helpers/message_formatter.py — Builds the broadcast-channel Telegram
message bodies in one place: the signal-cycle broadcast, its update notice,
and the blocked-signal warning.

The signal bodies are cycle-shaped, not signal-shaped: a trade owns a single
message that is re-rendered from its stored event history each time a new
action arrives (see ``broker/services/broadcast_service.py``), so there is no
"format one signal" entry point any more.

The owner-facing side of the same trade is not here: it is a living message
with buttons rather than a body, so it lives in
``broker/helpers/trade_card.py`` next to the keyboard that drives it. Both
render numbers through :func:`format_number`.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation

from broker.constants import SIGNAL_BLOCKED
from broker.domain.broadcast_status import merge_status, status_for_action
from broker.helpers import emoji_constants as em
from broker.helpers.signal_helper import action_to_emoji
from broker.helpers.timeframe_helper import format_timeframe
from broker.helpers.timezone_helper import format_notification_time
from broker.schemas.core import BroadcastStatusEnum, SignalActionEnum
from broker.schemas.webhook_schema import WebhookPayload


def format_number(value) -> str:
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
    parts.append(f"TP1%: {format_number(event.get('tp1_percent'))}%")
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


def _format_entry_block(event: dict) -> str:
  lines = []
  if event.get("price") is not None:
    lines.append(f"Price: {format_number(event.get('price'))}")
    
  qty_risk = []
  if event.get("quantity") is not None:
    qty_risk.append(f"Quantity: {format_number(event.get('quantity'))}")
  if event.get("risk_percent") is not None:
    qty_risk.append(f"Risk: {format_number(event.get('risk_percent'))}%")
  if qty_risk:
    lines.append(" | ".join(qty_risk))
    
  levels = []
  for name, key in (("SL", "sl"), ("TP1", "tp1"), ("TP2", "tp2")):
    if event.get(key) is not None:
      levels.append(f"{name}: {format_number(event.get(key))}")
  if levels:
    lines.append(" | ".join(levels))
    
  return "\n".join(lines)


def _format_action_block(event: dict, *, timezone_offset: str | None) -> str:
  action = _event_action(event)
  label = _enum_value(event.get("action"))
  icon = action_to_emoji(action) if action is not None else em.DEFAULT_SIGNAL
  
  lines = [f"{icon} {label}"]
  
  qty_risk = []
  if event.get("price") is not None:
    qty_risk.append(f"Price: {format_number(event.get('price'))}")
  if event.get("quantity") is not None:
    qty_risk.append(f"Quantity: {format_number(event.get('quantity'))}")
  if event.get("risk_percent") is not None:
    qty_risk.append(f"Risk: {format_number(event.get('risk_percent'))}%")
  if qty_risk:
    lines.append(" | ".join(qty_risk))
    
  stamp = _event_time(event.get("timestamp"), timezone_offset)
  attempt = event.get("attempt")
  if stamp:
    time_line = stamp
    if attempt:
      time_line += f" {em.CYCLE_RETRY} attempt {attempt}"
    lines.append(time_line)
    
  return "\n".join(lines)


def worker_label(worker) -> str:
  """Short, public-safe name for a worker row: gateway + masked account id.

  The account id is masked to its last four characters on purpose — the public
  broadcast shows *that* a worker took the signal and where it stands, and the
  owner recognises their own line, without publishing anyone's full account
  number to a channel.
  """
  account_id = str(getattr(worker, "account_id", "") or "")
  if len(account_id) > 10:
    masked = f"{account_id[:4]}****{account_id[-4:]}"
  elif len(account_id) > 4:
    masked = f"****{account_id[-4:]}"
  else:
    masked = account_id or "?"
    
  gateway = getattr(worker, "gateway", None)
  if not gateway:
    market = getattr(worker, "market", None)
    gateway = _enum_value(market) if market is not None else ""
  return f"{gateway} {masked}".strip()


def _format_worker_table(workers) -> str:
  """Two-column ``worker | latest status`` table of who executed the cycle.

  Columns line up because the whole broadcast body is sent inside a single
  ``<pre>`` box (see ``BroadcastNotifier``) — this does not open its own, since
  Telegram's HTML parser does not allow a ``<pre>`` nested inside another. The
  same table is re-rendered on every update, which is what makes the message
  readable as it fills in.
  """
  rows = [(worker_label(w), _enum_value(w.latest_status)) for w in workers or []]
  if not rows:
    return ""
  width = max(len(name) for name, _ in rows)
  width = max(width, len("ID"))
  header = f"{'Name'.ljust(width)}  Status"
  lines = "\n".join(f"{name.ljust(width)}  {status}" for name, status in rows)
  return f"Workers ({len(rows)})\n{header}\n{lines}"


def format_broadcast_message(
  record,
  *,
  workers=None,
  timezone_offset: str | None = None,
  include_raw: bool = False,
  include_meta: bool = False,
) -> str:
  events = [event for event in (record.events or []) if isinstance(event, dict)]
  status = record.status
  try:
    status = BroadcastStatusEnum(_enum_value(status))
  except ValueError:
    status = BroadcastStatusEnum.RUNNING

  status_icon = _STATUS_ICONS.get(status, em.CYCLE_RUNNING)
  timeframe = f" ({format_timeframe(record.timeframe)})" if record.timeframe else ""

  entry_event = events[0] if events else {}
  entry_action = _event_action(entry_event) if entry_event else None
  entry_icon = action_to_emoji(entry_action) if entry_action is not None else em.DEFAULT_SIGNAL
  entry_label = _enum_value(entry_event.get("action")) if entry_event else ""

  # Box 1: Position
  position_lines = []
  position_lines.append(f"[{status_icon}{status.value}]")
  if entry_label:
    position_lines.append(f"{entry_icon} {entry_label} {record.symbol}{timeframe}")
  else:
    position_lines.append(f"{entry_icon} {record.symbol}{timeframe}")
  position_lines.append(_BROADCAST_DIVIDER)
  if entry_event:
    entry_block = _format_entry_block(entry_event)
    if entry_block:
      position_lines.append(entry_block)
  position_lines.append(_BROADCAST_DIVIDER)
  position_box = "\n".join(position_lines)

  # Box 2: Actions
  action_events = events[1:]
  actions_box = ""
  if action_events:
    actions_lines = ["Actions:", _BROADCAST_DIVIDER]
    action_blocks = []
    for event in action_events:
      action_blocks.append(_format_action_block(event, timezone_offset=timezone_offset))
    actions_lines.append("\n\n".join(action_blocks))
    actions_lines.append(_BROADCAST_DIVIDER)
    actions_box = "\n</pre>\n<pre>" + "\n".join(actions_lines)

  # Box 3: Settings (Private)
  settings_box = ""
  if include_meta:
    settings_lines = ["Signal info:", _BROADCAST_DIVIDER]
    settings_lines.append(f"Strategy: {record.strategy}")
    settings_lines.append(f"Signal: {record.signal_uxid}")
    settings_lines.append(_BROADCAST_DIVIDER)

    flags = _broadcast_flags_line(entry_event) if entry_event else ""
    if flags:
      settings_lines.append(f"{em.BAR_CHART}Settings:")
      settings_lines.append(_BROADCAST_DIVIDER)
      settings_lines.append(flags)
      settings_lines.append(_BROADCAST_DIVIDER)

    if include_raw and events:
      raw = _format_raw_dicts(events[-1].get("indicators"), events[-1].get("inputs"))
      if raw:
        settings_lines.append(raw.lstrip("\n"))
        settings_lines.append(_BROADCAST_DIVIDER)

    settings_box = "\n</pre>\n<pre>" + "\n".join(settings_lines)

  # Box 4: Workers (Private)
  workers_box = ""
  if include_meta:
    executions = _format_worker_table(workers)
    if executions:
      workers_box = "\n</pre>\n<pre>" + executions

  return f"{position_box}{actions_box}{settings_box}{workers_box}"


def _status_through(events, index: int, fallback: BroadcastStatusEnum):
  """The cycle's status as of ``events[index]``.

  Folded from the events themselves rather than read off the row, because a
  notice is written *for one event*: when two events are delivered together
  (the dispatcher coalesces everything pending on a cycle), the row's status is
  already the final one and would mark an intermediate TP1 as CLOSED.
  """
  status = None
  for event in events[: index + 1]:
    action = _event_action(event)
    if action is None:
      continue
    incoming = status_for_action(action)
    status = incoming if status is None else merge_status(status, incoming)
  return status or fallback


def format_broadcast_update_notice(record, event_index: int) -> str | None:
  """The two-line reply posted under a cycle's message when it changes.

  The message itself is *edited* in place, so anyone who read it earlier never
  learns that the trade moved on — Telegram shows no notification for an edit.
  A reply does notify, and being a reply it points straight back at the full
  body, so it stays deliberately minimal: the cycle's status at that point and
  the action that produced it.

      [🏁CLOSED]
      🎯 TP1

  ``None`` when *event_index* names no event, so a caller can walk a range
  without first checking how many events the cycle holds.
  """
  events = [event for event in (record.events or []) if isinstance(event, dict)]
  if event_index < 0 or event_index >= len(events):
    return None

  row_status = record.status
  try:
    row_status = BroadcastStatusEnum(_enum_value(row_status))
  except ValueError:
    row_status = BroadcastStatusEnum.RUNNING

  status = _status_through(events, event_index, row_status)
  status_icon = _STATUS_ICONS.get(status, em.CYCLE_RUNNING)

  event = events[event_index]
  action = _event_action(event)
  label = _enum_value(event.get("action"))
  icon = action_to_emoji(action) if action is not None else em.DEFAULT_SIGNAL
  return f"[{status_icon}{status.value}]\n{icon} {label}".rstrip()


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
