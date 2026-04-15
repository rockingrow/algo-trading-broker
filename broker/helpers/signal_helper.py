"""
broker/helpers/signal_helper.py — Converts raw WebhookPayload into a validated TradingSignal.
"""

from __future__ import annotations

from broker.schemas.webhook_schema import TradingSignal, WebhookPayload
from broker.logger import get_logger

log = get_logger(__name__)


def parse_signal(payload: WebhookPayload, signal_id: str) -> TradingSignal:
  """
  Validate and normalise a raw TradingView webhook payload into a TradingSignal.

  The payload is structured according to the examples/*.json format.
  """
  position = payload.position
  action = position.action

  # The incoming action is already a SignalAction enum due to Pydantic validation
  # in WebhookPayload.

  # Normalise symbol (e.g. OANDA:XAUUSD -> XAUUSD)
  symbol = payload.symbol.split(":")[-1].upper().strip()

  # Use the first TP as the main target if available
  tp = position.tp1 or position.tp2

  # Action is already validated by Pydantic

  signal = TradingSignal(
    signal_id=signal_id,
    action=action,
    symbol=symbol,
    price=position.price,
    volume=position.quantity,
    sl=position.sl,
    tp=tp,
    comment="TV_Signal",
  )

  log.debug("Parsed signal: %s", signal.model_dump_json())
  return signal
