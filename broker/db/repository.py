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
from broker.schemas.trade_schema import TradeStatusEnum
from broker.schemas.trade_event_schema import PositionEvent
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
    indicators=json.loads(payload.indicators.model_dump_json())
    if payload.indicators is not None
    else {},
    inputs=json.loads(payload.inputs.model_dump_json())
    if payload.inputs is not None
    else {},
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


# Map worker position status → broker trade status.
_POSITION_STATUS_TO_TRADE_STATUS: dict[str, TradeStatusEnum] = {
  "OPENED": TradeStatusEnum.OPENED,
  "TP1": TradeStatusEnum.PARTIALLY_CLOSED,
  "TP2": TradeStatusEnum.CLOSED,
  "SL": TradeStatusEnum.CLOSED,
  "R_SL": TradeStatusEnum.CLOSED,
  "TERMINAL_CLOSED": TradeStatusEnum.CLOSED,
  "FORCED_CLOSED": TradeStatusEnum.CLOSED,
  "FLATTED": TradeStatusEnum.FLAT,
}

_OPEN_STATUSES = {"OPENED", "TP1"}

_STATUS_ORDER: dict[TradeStatusEnum, int] = {
  TradeStatusEnum.OPENED: 0,
  TradeStatusEnum.PARTIALLY_CLOSED: 1,
  TradeStatusEnum.CLOSED: 2,
  TradeStatusEnum.FLAT: 2,
  TradeStatusEnum.REJECTED: -1,
}


async def upsert_trade_by_position_event(event: PositionEvent) -> Trade | None:
  """Apply a PositionEvent received from the worker (via NATS TRADE) to the
  broker's `trades` table. Performs an upsert keyed by (account_id, ticket):
  updates the row if it exists, otherwise inserts a new one. Idempotent."""
  trade_status = _POSITION_STATUS_TO_TRADE_STATUS.get(event.status)
  if trade_status is None:
    log.warning(
      "upsert_trade_by_position_event: unknown position status=%s", event.status
    )
    return None

  is_running = event.status in _OPEN_STATUSES
  price = event.closed_price if event.closed_price is not None else event.opened_price

  async with get_session() as session:
    result = await session.execute(
      select(Trade).where(
        Trade.account_id == event.account_id,
        Trade.ticket == event.source_ticket,
      )
    )
    row: Optional[Trade] = result.scalars().first()

    if row is not None:
      if _STATUS_ORDER.get(trade_status, 0) < _STATUS_ORDER.get(row.status, 0):
        log.warning(
          "upsert_trade_by_position_event: ignoring status downgrade %s → %s "
          "for account_id=%s ticket=%s",
          row.status, trade_status, event.account_id, event.source_ticket,
        )
        return row
      row.status = trade_status
      row.is_running = is_running
      row.price = price
      row.quantity = event.volume
      if event.comment is not None:
        row.comment = event.comment
      if event.account_balance is not None:
        row.account_balance = event.account_balance
      await session.flush()
      await session.refresh(row)
      log.debug(
        "trade upserted (update) id=%s account_id=%s ticket=%s status=%s",
        str(row.id),
        event.account_id,
        event.source_ticket,
        trade_status,
      )
      return row

    if event.account_leverage is None:
      log.error(
        "upsert_trade_by_position_event: cannot create Trade for "
        "account_id=%s ticket=%s without account_leverage",
        event.account_id,
        event.source_ticket,
      )
      return None

    new_row = Trade(
      id=uuid.uuid4(),
      account_id=event.account_id,
      account_leverage=event.account_leverage,
      account_balance_init=event.account_balance,
      account_balance=event.account_balance,
      ticket=event.source_ticket,
      comment=event.comment,
      magic=event.magic or f"{event.action}|{event.signal_id or event.source_ticket}",
      strategy=event.strategy,
      symbol=event.symbol,
      action=event.action.upper(),
      price=price,
      quantity=event.volume,
      sl=event.sl,
      tp1=event.tp1,
      tp2=event.tp2,
      is_running=is_running,
      risk_percent=event.risk_percent,
      status=trade_status,
      reject_reason=None,
    )
    session.add(new_row)
    await session.flush()
    await session.refresh(new_row)
    log.debug(
      "trade upserted (insert) id=%s account_id=%s ticket=%s status=%s",
      str(new_row.id),
      event.account_id,
      event.source_ticket,
      trade_status,
    )
    return new_row
