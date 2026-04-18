"""
broker/apis/webhook.py — FastAPI router that receives TradingView webhook alerts.

Architecture:
  TradingView (Webhook) -> Broker (FastAPI) -> ZMQ PUB -> Subscribers
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request, status

from broker.db.repository import log_signal
from broker.schemas.webhook_schema import WebhookPayload
from broker.schemas.publisher_schema import TradingSignal
from broker.helpers.signal_helper import parse_signal, action_to_emoji
from broker.settings import settings
from broker.logger import get_logger
from broker.services.notification_service import TelegramNotification
from broker.helpers.timeframe_helper import format_timeframe

log = get_logger(__name__)


def get_webhook_router() -> APIRouter:
  router = APIRouter()

  @router.post("/webhook", status_code=status.HTTP_202_ACCEPTED, tags=["webhook"])
  async def receive_webhook(
    payload: WebhookPayload,
    request: Request,
  ) -> Dict[str, Any]:
    """
    Main entry point for incoming signals.
    1. Verify Token (inside JSON payload)
    2. Parse & Map Signal
    3. Publish to ZMQ
    4. Log to DB
    """
    # 1. Verify token
    """Raise 401 if the provided token does not match settings.WEBHOOK_SECRET."""
    if not settings.WEBHOOK_SECRET:
      raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Webhook secret not configured",
      )
    if payload.token != settings.WEBHOOK_SECRET:
      log.warning("Invalid token received in webhook payload")
      raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid token received in webhook payload",
      )

    log.info("Webhook payload: %s", {**payload.model_dump(), "token": "***"})

    # 2. Log to DB to generate signal_id
    db_signal_id = await log_signal(payload=payload)
    if not db_signal_id:
      raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Failed to persist signal into database",
      )

    # 3. Parse & validate mapping
    parse_error: str | None = None
    signal: TradingSignal | None = None
    try:
      signal = parse_signal(payload, db_signal_id)
    except Exception as exc:
      parse_error = str(exc)
      log.error("Signal parse error for %s: %s", payload.symbol, exc)

    if signal is None:
      raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=parse_error or "Signal could not be parsed.",
      )

    # 4. Publish
    publish_error: str | None = None
    published = False
    try:
      publisher = request.app.state.publisher
      publisher.publish(signal=signal)
      published = True
    except Exception as exc:
      publish_error = str(exc)
      log.error("ZMQ publish error: %s", exc)

    if not published:
      raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=f"Signal logged but publish failed: {publish_error}",
      )

    # 5. Send Telegram notification
    notification = TelegramNotification()

    # Log every indicator and input
    indicators_dict = payload.indicators.model_dump(exclude_none=True)
    for key, value in indicators_dict.items():
      log.info("Indicator Index -> %s: %s", key, value)

    inputs_dict = payload.inputs.model_dump(exclude_none=True)
    for key, value in inputs_dict.items():
      log.info("Input -> %s: %s", key, value)

    indicator_msg_lines = [f" {k}: <code>{v}</code>" for k, v in indicators_dict.items()]
    indicators_text = "\n".join(indicator_msg_lines)

    input_msg_lines = [f" {k}: <code>{v}</code>" for k, v in inputs_dict.items()]
    inputs_text = "\n".join(input_msg_lines)

    msg = (
      f"{action_to_emoji(payload.position.action)} <b>{payload.symbol}</b> ({format_timeframe(payload.timeframe)})\n"
      f"Action: <b>{payload.position.action.value}</b>\n"
      f"Price: <code>{payload.position.price}</code>\n"
      f"SL: <code>{payload.position.sl}</code> | TP1: <code>{payload.position.tp1}</code>\n"
      f"Time: {payload.timestamp.strftime('%Y-%m-%d %H:%M:%S')}\n"
      f"\n"
      f"-------------------------------------\n"
      f"<b>Indicators:</b>\n"
      f"{indicators_text}\n"
      f"\n"
      f"<b>Inputs:</b>\n"
      f"{inputs_text}"
    )
    notification.send_message(msg)

    return {
      "status": "accepted",
      "signal_id": signal.signal_id,
      "timestamp": signal.timestamp.isoformat(),
    }

  return router
