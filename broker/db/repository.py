"""
broker/db/repository.py
────────────────────────
Async write helpers — one function per DB operation.
These are thin wrappers around SQLAlchemy sessions so the
callers (webhook.py, trade_listener.py) stay clean.
"""

from __future__ import annotations

from broker.db.engine import get_session
from broker.db.models import SignalLog
from broker.schemas.webhook import WebhookPayload
from broker.logger import get_logger

log = get_logger(__name__)


async def log_signal(payload: WebhookPayload) -> None:
  """
  Persist a received TradingView webhook signal to the signal_log table.

  Parameters
  ----------
  payload : the validated WebhookPayload object
  """
  pos = payload.position
  row = SignalLog(
    token=payload.token,
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
    log.debug("signal_log written for symbol=%s", payload.symbol)
  except Exception as exc:
    log.error("Failed to write signal_log: %s", exc)
