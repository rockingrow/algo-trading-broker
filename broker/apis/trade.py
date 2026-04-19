"""
broker/apis/trade.py
────────────────────
FastAPI router that exposes Trade management endpoints consumed by worker nodes.

Endpoints
---------
POST  /trades          — Create a new Trade record
PATCH /trades/{id}     — Partially update an existing Trade record
"""

from __future__ import annotations

import uuid
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, status

from broker.db.repository import create_trade, update_trade
from broker.schemas.trade_schema import (
  TradeCreateRequest,
  TradeResponse,
  TradeUpdateRequest,
)
from broker.logger import get_logger

log = get_logger(__name__)


def get_trade_router() -> APIRouter:
  router = APIRouter(prefix="/trades", tags=["trades"])

  # ──────────────────────────────────────────────
  # POST /trades — create a new trade record
  # ──────────────────────────────────────────────
  @router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=TradeResponse,
    summary="Create a Trade",
    description=(
      "Persist a new Trade record as reported by a worker/execution node. "
      "Typically called immediately after the broker places an order."
    ),
  )
  async def create(payload: TradeCreateRequest) -> TradeResponse:
    """
    Create a new Trade row.

    - **signal_id**: UUID of the originating signal (FK to signals table)
    - **status**: Initial status — OPENED or REJECTED
    - Returns the full Trade record including generated `id` and timestamps.
    """
    log.info(
      "Creating trade: signal_id=%s symbol=%s action=%s status=%s",
      payload.signal_id,
      payload.symbol,
      payload.action,
      payload.status,
    )

    trade = await create_trade(payload)
    if trade is None:
      raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Failed to persist trade into database",
      )

    log.info("Trade created id=%s", str(trade.id))
    return trade

  # ──────────────────────────────────────────────
  # PATCH /trades/{signal_id} — update a trade record
  # ──────────────────────────────────────────────
  @router.patch(
    "/{signal_id}",
    status_code=status.HTTP_200_OK,
    response_model=TradeResponse,
    summary="Update a Trade",
    description=(
      "Partially update an existing Trade record using its signal_id. "
      "Only the fields present in the request body are modified. "
      "Typically called when the worker closes or partially closes a position."
    ),
  )
  async def patch(
    signal_id: uuid.UUID,
    payload: TradeUpdateRequest,
  ) -> TradeResponse:
    """
    Partially update a Trade row identified by *signal_id*.

    - Supply only the fields that have changed.
    - **status** transitions: OPENED → PARTIALLY_CLOSED → CLOSED (or REJECTED).
    - Returns the full updated Trade record.
    """
    log.info("Updating trade for signal_id=%s", str(signal_id))

    trade = await update_trade(signal_id=signal_id, payload=payload)
    if trade is None:
      raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Trade with signal_id {signal_id} not found",
      )

    log.info("Trade updated id=%s status=%s", str(trade.id), trade.status)
    return trade

  return router
