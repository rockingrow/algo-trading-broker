"""
broker/webhook.py — FastAPI application that receives TradingView webhook alerts.

Architecture:
  TradingView (Webhook) -> Broker (FastAPI) -> ZMQ PUB -> Subscribers
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import FastAPI, HTTPException, status

from broker.db.repository import log_signal
from broker.publisher import SignalPublisher
from broker.schemas.webhook import TradingSignal, WebhookPayload
from broker.signal_parser import parse_signal
from broker.settings import settings
from broker.logger import get_logger

log = get_logger(__name__)


def create_app(publisher: SignalPublisher) -> FastAPI:
  """Build and return the FastAPI application with all routes wired up."""

  app = FastAPI(
    title="Algo Trading Broker",
    description=(
      "Receives TradingView JSON webhook alerts, logs them to PostgreSQL, "
      "and fans them out via ZeroMQ PUB to subscriber VPS nodes."
    ),
    version="2.0.0",
  )

  # ── Helpers ────────────────────────────────────────────────────

  def _verify_token(token: str) -> None:
    """Raise 401 if the provided token does not match settings.WEBHOOK_SECRET."""
    if not settings.WEBHOOK_SECRET:
      return  # validation disabled

    if token != settings.WEBHOOK_SECRET:
      log.warning("Invalid token received in webhook payload")
      return  # skipped, no need to raise exception

  # ── Routes ─────────────────────────────────────────────────────

  @app.get("/health", tags=["system"])
  async def health() -> Dict[str, str]:
    return {"status": "ok"}

  @app.post("/webhook", status_code=status.HTTP_202_ACCEPTED, tags=["webhook"])
  async def receive_webhook(
    payload: WebhookPayload,
  ) -> Dict[str, Any]:
    """
    Main entry point for incoming signals.
    1. Verify Token (inside JSON payload)
    2. Parse & Map Signal
    3. Publish to ZMQ
    4. Log to DB
    """
    # 1. Verify token
    _verify_token(payload.token)

    log.info("Webhook received for symbol: %s", payload.symbol)

    # 2. Parse & validate mapping
    parse_error: str | None = None
    signal: TradingSignal | None = None
    try:
      signal = parse_signal(payload)
    except Exception as exc:
      parse_error = str(exc)
      log.error("Signal parse error for %s: %s", payload.symbol, exc)

    if signal is None:
      raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=parse_error or "Signal could not be parsed.",
      )

    # 3. Publish
    publish_error: str | None = None
    published = False
    try:
      publisher.publish(signal)
      published = True
    except Exception as exc:
      publish_error = str(exc)
      log.error("ZMQ publish error: %s", exc)

    # 4. Log to DB
    await log_signal(payload=payload)

    if not published:
      raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=f"Signal logged but publish failed: {publish_error}",
      )

    return {
      "status": "accepted",
      "signal_id": signal.signal_id,
      "timestamp": signal.timestamp.isoformat(),
    }

  return app
