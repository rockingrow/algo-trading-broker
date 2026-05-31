"""
broker/helpers/message_formatter.py — Builds the Telegram message bodies for
webhook events in one place, so the webhook flow no longer duplicates the
formatting between the FLAT and the normal-signal branches.
"""

from __future__ import annotations

from broker.constants import SIGNAL_BLOCKED
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
    f"Action: <b>FLAT</b>\n"
    f"Time: {payload.timestamp.strftime(_TIME_FMT)}\n"
  )


def format_signal_message(payload: WebhookPayload) -> str:
  """Telegram body for a normal entry / target / stop signal."""
  pos = payload.position
  return (
    f"{_header(payload)}"
    f"Action: <b>{pos.action.value}</b>\n"
    f"Price: <code>{pos.price}</code>\n"
    f"Quantity: <code>{pos.quantity}</code>\n"
    f"SL: <code>{pos.sl}</code> | TP1: <code>{pos.tp1}</code> | "
    f"TP2: <code>{pos.tp2}</code>\n"
    f"Time: {payload.timestamp.strftime(_TIME_FMT)}\n"
  )


def format_blocked_message(payload: WebhookPayload) -> str:
  """Telegram body sent when signal processing is disabled."""
  return (
    f"🚫 <b>Broker signal blocked</b>\n"
    f"Symbol: <b>{payload.symbol}</b>\n"
    f"Reason: Signal processing is temporarily disabled "
    f"(<code>{SIGNAL_BLOCKED}</code> != 1)"
  )
