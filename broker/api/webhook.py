"""
broker/api/webhook.py — FastAPI router that receives TradingView webhook alerts.

Architecture:
  TradingView (Webhook) -> Broker (FastAPI) -> NATS -> Subscribers

The route is intentionally thin: it delegates the whole pipeline (auth, block
check, persist, publish, notify) to ``SignalProcessingService`` and only
translates the service's ``SignalError`` into an HTTP response.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, status

from broker.providers import get_signal_service
from broker.logger import get_logger
from broker.schemas.webhook_schema import WebhookPayload
from broker.services.signal_processing_service import (
  SignalError,
  SignalProcessingService,
)

log = get_logger(__name__)


def get_webhook_router() -> APIRouter:
  router = APIRouter()

  @router.post(
    "/webhook",
    status_code=status.HTTP_202_ACCEPTED,
    tags=["webhook"],
    summary="Receive a TradingView alert",
    responses={
      202: {"description": "Signal accepted, persisted and queued on JetStream."},
      401: {"description": "Invalid webhook `token`."},
      403: {"description": "Signals are currently blocked by the broker."},
    },
  )
  async def receive_webhook(
    payload: WebhookPayload,
    service: SignalProcessingService = Depends(get_signal_service),
  ) -> Dict[str, Any]:
    """Main entry point for incoming signals.

    Authenticated via the `token` field inside the JSON body (not the
    `X-API-KEY` header). The route only runs the fast path — verify, block
    check, persist (``status=QUEUED``), enqueue on JetStream — so TradingView
    is not held open across the fan-out. The background ``SignalWorker`` picks
    the envelope up and runs publish + notify + mark PUBLISHED.
    """
    try:
      return await service.process(payload)
    except SignalError as exc:
      raise HTTPException(status_code=exc.status_code, detail=exc.detail)

  return router
