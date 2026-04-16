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

  # Normalise symbol (e.g. OANDA:XAUUSD -> XAUUSD)
  symbol = payload.symbol.split(":")[-1].upper().strip()

  # Action is already validated by Pydantic
  signal = TradingSignal(
    signal_id=signal_id,
    action=action,
    symbol=symbol,
    price=position.price,
    quantity=position.quantity,
    sl=position.sl,
    tp1=position.tp1,
    tp2=position.tp2,
    is_running=position.is_running if position.is_running is not None else False,
    risk_percent=payload.inputs.risk_percent
    if payload.inputs.risk_percent is not None
    else 0.0,
  )

  log.debug("Parsed signal: %s", signal.model_dump_json())
  return signal
