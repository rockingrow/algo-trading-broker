"""
broker/helpers/signal_helper.py — Converts raw WebhookPayload into a validated TradingSignal.
"""

from __future__ import annotations

from broker.schemas.webhook_schema import WebhookPayload
from broker.schemas.publisher_schema import TradingSignal
from broker.logger import get_logger
from broker.schemas.core import SignalActionEnum

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
    strategy=payload.strategy,
    action=action,
    symbol=symbol,
    price=position.price or 0.0,
    quantity=position.quantity or 0.0,
    sl=position.sl,
    tp1=position.tp1,
    tp2=position.tp2,
    is_running=position.is_running if position.is_running is not None else False,
    risk_percent=payload.inputs.risk_percent
    if payload.inputs is not None and payload.inputs.risk_percent is not None
    else 0.0,
  )

  log.debug("Parsed signal: %s", signal.model_dump_json())
  return signal


_ACTION_EMOJI: dict[SignalActionEnum, str] = {
  SignalActionEnum.LONG: "🟢",
  SignalActionEnum.SHORT: "🔴",
  SignalActionEnum.TP1: "🎯",
  SignalActionEnum.TP2: "🚀",
  SignalActionEnum.R_SL: "🛡️",
  SignalActionEnum.SL: "❌",
  SignalActionEnum.FLAT: "🏳️",
}


def action_to_emoji(action: SignalActionEnum) -> str:
  """
  Convert a SignalActionEnum to a Telegram emoji. Adding a new action only
  requires extending ``_ACTION_EMOJI`` — no control-flow changes.
  """
  return _ACTION_EMOJI.get(action, "📡")
