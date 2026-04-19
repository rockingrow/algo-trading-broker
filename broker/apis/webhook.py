"""
broker/apis/webhook.py — FastAPI router that receives TradingView webhook alerts.

Architecture:
  TradingView (Webhook) -> Broker (FastAPI) -> NATS -> Subscribers
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request, status

from broker.db.repository import log_signal
from broker.schemas.webhook_schema import WebhookPayload
from broker.schemas.publisher_schema import TradingSignal
from broker.schemas.core import SignalActionEnum
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
    3. Publish to NATS
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

    # 3a. FLAT — close-all shortcut: publish directive then notify
    if payload.position.action == SignalActionEnum.FLAT:
      flat_symbol = payload.symbol.split(":")[-1].upper().strip()
      publish_error: str | None = None
      try:
        publisher = request.app.state.publisher
        await publisher.publish_flat(symbol=flat_symbol, timestamp=payload.timestamp)
      except Exception as exc:
        publish_error = str(exc)
        log.exception("NATS publish_flat error: %s", exc)

      if publish_error:
        raise HTTPException(
          status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
          detail=f"Signal logged but publish failed: {publish_error}",
        )

      notification = TelegramNotification(chat_id=settings.TELEGRAM_CHAT_CHANNEL_ID or settings.TELEGRAM_CHAT_ID)
      msg = (
        f"-------------------------------------\n"
        f"\n"
        f"{action_to_emoji(payload.position.action)} <b>{payload.symbol}</b> ({format_timeframe(payload.timeframe)})\n"
        f"Action: <b>FLAT</b>\n"
        f"Time: {payload.timestamp.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"\n"
        f"-------------------------------------\n"
      )
      notification.send_message(msg)

      return {
        "status": "accepted",
        "signal_id": db_signal_id,
        "timestamp": payload.timestamp.isoformat(),
      }

    # 3b. Parse & validate mapping
    parse_error: str | None = None
    signal: TradingSignal | None = None
    try:
      signal = parse_signal(payload, db_signal_id)
    except Exception as exc:
      parse_error = str(exc)
      log.exception("Signal parse error for %s: %s", payload.symbol, exc)

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
      await publisher.publish(signal=signal)
      published = True
    except Exception as exc:
      publish_error = str(exc)
      log.exception("NATS publish error: %s", exc)

    if not published:
      raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=f"Signal logged but publish failed: {publish_error}",
      )

    # 5. Send Telegram notification
    notification = TelegramNotification(chat_id=settings.TELEGRAM_CHAT_CHANNEL_ID or settings.TELEGRAM_CHAT_ID)
    msg = (
      f"-------------------------------------\n"
      f"\n"
      f"{action_to_emoji(payload.position.action)} <b>{payload.symbol}</b> ({format_timeframe(payload.timeframe)})\n"
      f"Action: <b>{payload.position.action.value}</b>\n"
      f"Price: <code>{payload.position.price}</code>\n"
      f"Quantity: <code>{payload.position.quantity}</code>\n"
      f"SL: <code>{payload.position.sl}</code> | TP1: <code>{payload.position.tp1}</code> | TP2: <code>{payload.position.tp2}</code>\n"
      f"Time: {payload.timestamp.strftime('%Y-%m-%d %H:%M:%S')}\n"
      f"\n"
      f"-------------------------------------\n"
    )
    notification.send_message(msg)

    return {
      "status": "accepted",
      "signal_id": signal.signal_id,
      "timestamp": signal.timestamp.isoformat(),
    }

  return router
