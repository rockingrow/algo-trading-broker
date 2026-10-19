"""
broker/helpers/signal_helper.py — Converts raw WebhookPayload into a validated TradingSignal.
"""

from __future__ import annotations

from broker.schemas.webhook_schema import WebhookPayload
from broker.schemas.publisher_schema import ScalingSchema, TradingSignal
from broker.logger import get_logger
from broker.schemas.core import SignalActionEnum
from broker.helpers import emoji_constants as em

log = get_logger(__name__)


def parse_signal(payload: WebhookPayload, signal_id: str) -> TradingSignal:
  """
  Validate and normalise a raw TradingView webhook payload into a TradingSignal.

  The payload is structured according to the examples/*.json format.

  Two ids travel to the worker and they answer different questions:

  * ``signal_id`` — this one signal, minted per persisted signal (the
    ``signals`` row id). Unique per action, and therefore the de-duplication
    key a worker uses to recognise a signal it has already acted on.
  * ``signal_uxid`` — the trade **cycle** the signal belongs to, shared by the
    entry and every TP/SL/FLAT that follows it, so a worker can tie a close
    back to the position it opened.
  """
  position = payload.position
  action = position.action

  # Normalise symbol (e.g. OANDA:XAUUSD -> XAUUSD)
  symbol = payload.symbol.split(":")[-1].upper().strip()

  # Only carry scaling information when the webhook explicitly flags a scale-in.
  is_scale_position = position.is_scale_position
  scaling = (
    ScalingSchema(**position.scaling.model_dump())
    if is_scale_position and position.scaling is not None
    else None
  )

  # Action is already validated by Pydantic
  signal = TradingSignal(
    signal_id=signal_id,
    signal_uxid=payload.signal_uxid,
    strategy=payload.strategy,
    action=action,
    symbol=symbol,
    price=position.price or 0.0,
    quantity=position.quantity or 0.0,
    sl=position.sl,
    tp1=position.tp1,
    tp2=position.tp2,
    tp1_percent=position.tp1_percent,
    move_sl_to_be=position.move_sl_to_be,
    is_running=position.is_running if position.is_running is not None else False,
    risk_percent=position.risk_percent
    if position.risk_percent is not None
    else (
      payload.inputs.risk_percent
      if payload.inputs is not None and payload.inputs.risk_percent is not None
      else 0.0
    ),
    use_equity_sizing=position.use_equity_sizing,
    is_scale_position=is_scale_position,
    scale_strategy=position.scale_strategy if is_scale_position else None,
    scaling=scaling,
  )

  log.debug("Parsed signal: %s", signal.model_dump_json())
  return signal


_ACTION_EMOJI: dict[SignalActionEnum, str] = {
  SignalActionEnum.LONG: em.LONG,
  SignalActionEnum.SHORT: em.SHORT,
  SignalActionEnum.TP1: em.TP1,
  SignalActionEnum.TP2: em.TP2,
  SignalActionEnum.R_SL: em.R_SL,
  SignalActionEnum.SL: em.SL,
  SignalActionEnum.FLAT: em.FLAT,
}


def action_to_emoji(action: SignalActionEnum) -> str:
  """
  Convert a SignalActionEnum to a Telegram emoji. Adding a new action only
  requires extending ``_ACTION_EMOJI`` — no control-flow changes.
  """
  return _ACTION_EMOJI.get(action, em.DEFAULT_SIGNAL)
