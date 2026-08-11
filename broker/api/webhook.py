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
      202: {
        "description": (
          "Signal accepted. `status=queued` when JetStream ack-ed the write "
          "inside `WEBHOOK_ENQUEUE_TIMEOUT`, `status=deferred` when the ack "
          "was too slow and the enqueue is being retried in the background."
        )
      },
      401: {"description": "Invalid webhook `token`."},
      503: {"description": "Enqueue failed and could not be deferred."},
    },
  )
  async def receive_webhook(
    payload: WebhookPayload,
    service: SignalProcessingService = Depends(get_signal_service),
  ) -> Dict[str, Any]:
    """Main entry point for incoming signals.

    Authenticated via the `token` field inside the JSON body (not the
    `X-API-KEY` header). The route only verifies the token and pushes the
    raw envelope onto JetStream, so TradingView gets its ``202`` back as
    soon as the message is durably queued. Every other step — block gate,
    persistence, publish to workers, notification, retries — runs in the
    background ``SignalWorker`` and, on failure, the periodic retry job.

    The wait for JetStream is bounded by ``WEBHOOK_ENQUEUE_TIMEOUT``: past it
    the service defers the enqueue to a background retry and still answers
    ``202``, because TradingView abandons a slow delivery ("request took too
    long and timed out") and never re-sends the alert.
    """
    try:
      return await service.enqueue(payload)
    except SignalError as exc:
      raise HTTPException(status_code=exc.status_code, detail=exc.detail)

  return router
