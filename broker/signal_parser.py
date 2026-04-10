"""
broker/signal_parser.py — Converts raw WebhookPayload into a validated TradingSignal.
"""
from __future__ import annotations

from broker.models import OrderDirection, SignalAction, TradingSignal, WebhookPayload
from shared.config import settings
from shared.logger import get_logger

log = get_logger(__name__)


def parse_signal(payload: WebhookPayload) -> TradingSignal:
    """
    Validate and normalise a raw TradingView webhook payload into a TradingSignal.

    Raises:
        ValueError: if required fields are missing or invalid.
    """
    action_raw = payload.action.lower().strip()
    try:
        action = SignalAction(action_raw)
    except ValueError:
        raise ValueError(
            f"Unknown action '{action_raw}'. "
            f"Supported: {[a.value for a in SignalAction]}"
        )

    symbol = payload.symbol.upper().strip()

    direction: OrderDirection | None = None
    if payload.direction:
        try:
            direction = OrderDirection(payload.direction.lower().strip())
        except ValueError:
            raise ValueError(
                f"Unknown direction '{payload.direction}'. Must be 'buy' or 'sell'."
            )

    if action == SignalAction.open and direction is None:
        raise ValueError("'direction' (buy/sell) is required for action='open'.")

    volume = payload.volume if payload.volume is not None else settings.DEFAULT_VOLUME
    if volume <= 0:
        raise ValueError(f"Volume must be > 0, got {volume}")

    signal = TradingSignal(
        action=action,
        symbol=symbol,
        direction=direction,
        volume=volume,
        sl=payload.sl,
        tp=payload.tp,
        ticket=payload.ticket,
        comment=payload.comment or "TV_Signal",
        magic=payload.magic or settings.INSTRUMENT_MAGIC,
    )

    log.debug("Parsed signal: %s", signal.model_dump_json())
    return signal
