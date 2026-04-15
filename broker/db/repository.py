"""
broker/db/repository.py
────────────────────────
Async write helpers — one function per DB operation.
These are thin wrappers around SQLAlchemy sessions so the
callers (webhook.py, trade_listener.py) stay clean.
"""

from __future__ import annotations

import uuid

from broker.db.engine import get_session
from broker.db.models import Signal
from broker.schemas.webhook_schema import WebhookPayload
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
