"""
broker/db/repository.py
────────────────────────
Async write helpers — one function per DB operation.
These are thin wrappers around SQLAlchemy sessions so the
callers (webhook.py, trade_listener.py) stay clean.
"""

from __future__ import annotations

import json
import uuid
from typing import Optional

from sqlalchemy import select

from broker.db.engine import get_session
from broker.db.models import Signal, Trade
from broker.schemas.webhook_schema import WebhookPayload
from broker.schemas.trade_schema import TradeCreateRequest, TradeUpdateRequest
from broker.logger import get_logger

log = get_logger(__name__)


async def log_signal(payload: WebhookPayload) -> str | None:
  """
  Persist a received TradingView webhook signal to the signals table.

  Returns
  -------
  str | None
      The UUID of the inserted row, or None if the insert failed.
  """
  pos = payload.position
  new_id = uuid.uuid4()
  row = Signal(
    id=new_id,
    strategy=payload.strategy,
    symbol=payload.symbol,
    timeframe=payload.timeframe,
    timestamp=payload.timestamp,
    action=pos.action,
    price=pos.price or 0.0,
    quantity=pos.quantity or 0.0,
    sl=pos.sl,
    tp1=pos.tp1,
    tp2=pos.tp2,
    is_running=pos.is_running if pos.is_running is not None else False,
    risk_percent=payload.inputs.risk_percent
    if payload.inputs is not None and payload.inputs.risk_percent is not None
    else 0.0,
    indicators=json.loads(payload.indicators.model_dump_json()) if payload.indicators is not None else {},
    inputs=json.loads(payload.inputs.model_dump_json()) if payload.inputs is not None else {},
    raw=json.loads(payload.model_dump_json()),
  )
  try:
    async with get_session() as session:
      session.add(row)
    log.debug("signals written for symbol=%s id=%s", payload.symbol, str(new_id))
    return str(new_id)
  except Exception as exc:
    log.exception("Failed to write signals: %s", exc)
    return None


async def create_trade(payload: TradeCreateRequest) -> Trade | None:
  """
  Persist a new Trade row to the trades table.

  Returns
  -------
  Trade | None
      The inserted ORM instance (with id/timestamps populated), or None on failure.
  """
  new_id = uuid.uuid4()
  row = Trade(
    id=new_id,
    account_id=payload.account_id,
    account_leverage=payload.account_leverage,
    account_balance_init=payload.account_balance_init,
    account_balance=payload.account_balance,
    ticket=payload.ticket,
    comment=payload.comment,
    magic=payload.magic,
    strategy=payload.strategy,
    symbol=payload.symbol,
    action=payload.action,
    price=payload.price,
    quantity=payload.quantity,
    sl=payload.sl,
    tp1=payload.tp1,
    tp2=payload.tp2,
    is_running=payload.is_running,
    risk_percent=payload.risk_percent,
    status=payload.status,
    reject_reason=payload.reject_reason,
  )
  try:
    async with get_session() as session:
      session.add(row)
      # Flush so the DB assigns server defaults (createdAt/updatedAt) and
      # makes the row visible within the same transaction before commit.
      await session.flush()
      await session.refresh(row)
    log.debug("trade written id=%s symbol=%s", str(new_id), row.symbol)
    return row
  except Exception as exc:
    log.exception("Failed to write trade: %s", exc)
    return None


async def update_trade(
  account_id: str, ticket: int, payload: TradeUpdateRequest
) -> Trade | None:
  """
  Apply a partial update to an existing Trade row, identified by account_id + ticket.

  Only non-None fields in *payload* are written to the database.

  Returns
  -------
  Trade | None
      The refreshed ORM instance after update, or None if not found / on failure.
  """
  try:
    async with get_session() as session:
      result = await session.execute(
        select(Trade).where(Trade.account_id == account_id, Trade.ticket == ticket)
      )
      row: Optional[Trade] = result.scalars().first()

      if row is None:
        log.warning(
          "update_trade: trade for account_id=%s ticket=%s not found",
          account_id,
          ticket,
        )
        return None

      update_data = payload.model_dump(exclude_none=True)
      for field, value in update_data.items():
        setattr(row, field, value)

      await session.flush()
      await session.refresh(row)

    log.debug(
      "trade updated id=%s account_id=%s ticket=%s", str(row.id), account_id, ticket
    )
    return row
  except Exception as exc:
    log.exception(
      "Failed to update trade for account_id=%s ticket=%s: %s", account_id, ticket, exc
    )
    return None
