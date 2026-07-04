"""
broker/helpers/message_formatter.py — Builds the Telegram message bodies for
webhook events in one place, so the webhook flow no longer duplicates the
formatting between the FLAT and the normal-signal branches.
"""

from __future__ import annotations

from broker.constants import SIGNAL_BLOCKED
from broker.helpers import emoji_constants as em
from broker.helpers.signal_helper import action_to_emoji
from broker.helpers.timeframe_helper import format_timeframe
from broker.schemas.webhook_schema import WebhookPayload

_TIME_FMT = "%Y-%m-%d %H:%M:%S"


def _header(payload: WebhookPayload) -> str:
  return (
    f"{action_to_emoji(payload.position.action)} <b>{payload.symbol}</b> "
    f"({format_timeframe(payload.timeframe)})\n"
  )


def format_flat_message(payload: WebhookPayload) -> str:
  """Telegram body for a FLAT (close-all) directive."""
  return (
    f"{_header(payload)}"
    f"Strategy: <b>{payload.strategy}</b>\n"
    f"Action: <b>FLAT</b>\n"
    f"Time: {payload.timestamp.strftime(_TIME_FMT)}\n"
  )


def _format_raw_section(payload: WebhookPayload) -> str:
  """Append indicators and inputs blocks when NOTIFICATION_INCLUDE_SIGNAL_RAW is on."""
  parts: list[str] = []
  if payload.indicators is not None:
    data = {k: v for k, v in payload.indicators.model_dump().items() if v is not None}
    if data:
      lines = "\n".join(f"  {k}: {v}" for k, v in data.items())
      parts.append(f"Indicators:\n{lines}")
  if payload.inputs is not None:
    data = {k: v for k, v in payload.inputs.model_dump().items() if v is not None}
    if data:
      lines = "\n".join(f"  {k}: {v}" for k, v in data.items())
      parts.append(f"Inputs:\n{lines}")
  return ("\n" + "\n".join(parts)) if parts else ""


def _format_position_flags_section(payload: WebhookPayload) -> str:
  """Section showing optional position state flags, wrapped in dashes."""
  pos = payload.position
  lines: list[str] = []

  def _flag(v: bool) -> str:
    return f"{em.FLAG_ON}" if v else f"{em.FLAG_OFF}"

  if pos.tp1_percent is not None:
    lines.append(f"TP1%: {_flag(True)} {pos.tp1_percent}%")
  if pos.move_sl_to_be is not None:
    lines.append(f"Move SL to BE: {_flag(pos.move_sl_to_be)}")
  if pos.is_running is not None:
    lines.append(f"Is Running: {_flag(pos.is_running)}")
  if pos.is_scale_position is not None:
    lines.append(
      f"Scale Position: {_flag(pos.is_scale_position)} {pos.scale_strategy if pos.scale_strategy is not None else ''}"
    )

  if not lines:
    return ""

  divider = "-----------"
  return f"{divider}\n" + "\n".join(lines) + f"\n{divider}\n"


def format_signal_message(payload: WebhookPayload, *, include_raw: bool = False) -> str:
  """Telegram body for a normal entry / target / stop signal."""
  pos = payload.position
  risk_percent = (
    pos.risk_percent
    if pos.risk_percent is not None
    else (
      payload.inputs.risk_percent
      if payload.inputs is not None and payload.inputs.risk_percent is not None
      else None
    )
  )
  risk_str = (
    f" | Risk: <code>{risk_percent}%</code>" if risk_percent is not None else ""
  )
  base = (
    f"{_header(payload)}"
    f"Strategy: <b>{payload.strategy}</b>\n"
    f"Action: <b>{pos.action.value}</b>\n"
    f"Price: <code>{pos.price}</code>\n"
    f"Quantity: <code>{pos.quantity}</code>{risk_str}\n"
    f"SL: <code>{pos.sl}</code> | TP1: <code>{pos.tp1}</code> | "
    f"TP2: <code>{pos.tp2}</code>\n"
    f"Time: {payload.timestamp.strftime(_TIME_FMT)}\n"
  )
  flags = _format_position_flags_section(payload)
  raw = _format_raw_section(payload) if include_raw else ""
  return base + (f"\n{flags}" if flags else "") + raw


def format_blocked_message(payload: WebhookPayload) -> str:
  """Telegram body sent when signal processing is disabled."""
  return (
    f"{em.BLOCKED} <b>Broker signal blocked</b>\n"
    f"Symbol: <b>{payload.symbol}</b>\n"
    f"Reason: Signal processing is temporarily disabled "
    f"(<code>{SIGNAL_BLOCKED}</code> != 1)"
  )
