"""
broker/apis/trade.py
────────────────────
FastAPI router that exposes Trade management endpoints consumed by worker nodes.

Endpoints
---------
POST  /trades                          — Create a new Trade record
PATCH /trades/{account_id}/{ticket}    — Partially update an existing Trade record
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader

from broker.db.repository import create_trade, update_trade
from broker.schemas.trade_schema import (
  TradeCreateRequest,
  TradeResponse,
  TradeUpdateRequest,
)
from broker.logger import get_logger
from broker.settings import settings

log = get_logger(__name__)

api_key_header = APIKeyHeader(name="X-API-KEY")


def verify_api_key(api_key: str = Security(api_key_header)) -> str:
  if api_key != settings.BROKER_API_KEY:
    raise HTTPException(
      status_code=status.HTTP_401_UNAUTHORIZED,
      detail="Invalid API key",
    )
  return api_key


def get_trade_router() -> APIRouter:
  router = APIRouter(
    prefix="/trades",
    tags=["trades"],
    dependencies=[Depends(verify_api_key)],
  )

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

    - **status**: Initial status — OPENED or REJECTED
    - Returns the full Trade record including generated `id` and timestamps.
    """
    log.info(
      "Creating trade: account_id=%s symbol=%s action=%s status=%s",
      payload.account_id,
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

  # ──────────────────────────────────────────────────────────
  # PATCH /trades/{account_id}/{ticket} — update a trade record
  # ──────────────────────────────────────────────────────────
  @router.patch(
    "/{account_id}/{ticket}",
    status_code=status.HTTP_200_OK,
    response_model=TradeResponse,
    summary="Update a Trade",
    description=(
      "Partially update an existing Trade record identified by account_id + ticket. "
      "Only the fields present in the request body are modified. "
      "Typically called when the worker closes or partially closes a position."
    ),
  )
  async def patch(
    account_id: str,
    ticket: int,
    payload: TradeUpdateRequest,
  ) -> TradeResponse:
    """
    Partially update a Trade row identified by *account_id* + *ticket*.

    - Supply only the fields that have changed.
    - **status** transitions: OPENED → PARTIALLY_CLOSED → CLOSED (or REJECTED).
    - Returns the full updated Trade record.
    """
    log.info("Updating trade for account_id=%s ticket=%s", account_id, ticket)

    trade = await update_trade(account_id=account_id, ticket=ticket, payload=payload)
    if trade is None:
      raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Trade with account_id={account_id} ticket={ticket} not found",
      )

    log.info("Trade updated id=%s status=%s", str(trade.id), trade.status)
    return trade

  return router
