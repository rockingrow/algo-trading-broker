"""
broker/apis/webhook.py — FastAPI router that receives TradingView webhook alerts.

Architecture:
  TradingView (Webhook) -> Broker (FastAPI) -> ZMQ PUB -> Subscribers
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request, status

from broker.db.repository import log_signal
from broker.schemas.webhook_schema import TradingSignal, WebhookPayload
from broker.helpers.signal_helper import parse_signal
from broker.settings import settings
from broker.logger import get_logger
from broker.services.notification_service import TelegramNotification

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
      publisher.publish(signal)
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
    if settings.TELEGRAM_ENABLED:
      notification = TelegramNotification()
      notification.send_message(str(payload))

    return {
      "status": "accepted",
      "signal_id": signal.signal_id,
      "timestamp": signal.timestamp.isoformat(),
    }

  return router
