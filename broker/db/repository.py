"""
broker/db/repository.py
────────────────────────
Async write helpers — one function per DB operation.
These are thin wrappers around SQLAlchemy sessions so the
callers (webhook.py, trade_listener.py) stay clean.
"""

from __future__ import annotations

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
    symbol=payload.symbol,
    timeframe=payload.timeframe,
    timestamp=payload.timestamp,
    action=pos.action,
    price=pos.price,
    quantity=pos.quantity,
    sl=pos.sl,
    tp1=pos.tp1,
    tp2=pos.tp2,
    is_running=pos.is_running if pos.is_running is not None else False,
    risk_percent=payload.inputs.risk_percent
    if payload.inputs.risk_percent is not None
    else 0.0,
    indicators=payload.indicators.model_dump(),
    inputs=payload.inputs.model_dump(),
  )
  try:
    async with get_session() as session:
      session.add(row)
    log.debug("signals written for symbol=%s id=%s", payload.symbol, str(new_id))
    return str(new_id)
  except Exception as exc:
    log.error("Failed to write signals: %s", exc)
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
    signal_id=payload.signal_id,
    account_id=payload.account_id,
    account_leverage=payload.account_leverage,
    account_balance_init=payload.account_balance_init,
    account_balance=payload.account_balance,
    ticket=payload.ticket,
    comment=payload.comment,
    magic=payload.magic,
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
    log.error("Failed to write trade: %s", exc)
    return None


async def update_trade(signal_id: uuid.UUID, payload: TradeUpdateRequest) -> Trade | None:
  """
  Apply a partial update to an existing Trade row, identified by its signal_id.

  Only non-None fields in *payload* are written to the database.

  Returns
  -------
  Trade | None
      The refreshed ORM instance after update, or None if not found / on failure.
  """
  try:
    async with get_session() as session:
      result = await session.execute(select(Trade).where(Trade.signal_id == signal_id))
      row: Optional[Trade] = result.scalars().first()

      if row is None:
        log.warning("update_trade: trade for signal_id=%s not found", str(signal_id))
        return None

      # Apply only the fields explicitly provided (non-None)
      update_data = payload.model_dump(exclude_none=True)
      for field, value in update_data.items():
        setattr(row, field, value)

      await session.flush()
      await session.refresh(row)

    log.debug("trade updated id=%s based on signal_id=%s", str(row.id), str(signal_id))
    return row
  except Exception as exc:
    log.error("Failed to update trade for signal_id=%s: %s", str(signal_id), exc)
    return None
