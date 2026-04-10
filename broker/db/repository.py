"""
broker/db/repository.py
────────────────────────
Async write helpers — one function per DB operation.
These are thin wrappers around SQLAlchemy sessions so the
callers (webhook.py, trade_listener.py) stay clean.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict

from broker.db.engine import get_session
from broker.db.models import SignalLog, TradeEvent
from broker.models import TradingSignal
from shared.logger import get_logger

log = get_logger(__name__)


async def log_signal(
    signal: TradingSignal,
    raw_payload: Dict[str, Any],
    published: bool,
    error: str | None = None,
) -> None:
    """
    Persist a received TradingView webhook signal to the signal_log table.

    Parameters
    ----------
    signal      : the parsed/validated TradingSignal object
    raw_payload : original dict from the HTTP request body
    published   : True if ZMQ publish succeeded
    error       : error message if something went wrong
    """
    row = SignalLog(
        signal_id=signal.signal_id,
        received_at=signal.timestamp,
        action=signal.action,
        symbol=signal.symbol,
        direction=signal.direction,
        volume=signal.volume,
        sl=signal.sl,
        tp=signal.tp,
        ticket=signal.ticket,
        comment=signal.comment,
        magic=signal.magic,
        raw_payload=raw_payload,
        published=published,
        error=error,
    )
    try:
        async with get_session() as session:
            session.add(row)
        log.debug("signal_log written: signal_id=%s", signal.signal_id)
    except Exception as exc:
        log.error("Failed to write signal_log: %s", exc)


async def log_trade_event(data: Dict[str, Any]) -> None:
    """
    Persist a trade event reported by a subscriber to the trade_event table.

    Parameters
    ----------
    data : raw dict from the ZMQ PUSH message (already parsed JSON)
    """
    # Parse optional ISO timestamp from subscriber
    event_ts: datetime | None = None
    if ts_str := data.get("timestamp"):
        try:
            event_ts = datetime.fromisoformat(ts_str)
        except ValueError:
            pass

    row = TradeEvent(
        received_at=datetime.now(timezone.utc),
        event_type=data.get("event_type", "unknown"),
        signal_id=data.get("signal_id"),
        subscriber_id=data.get("subscriber_id"),
        ticket=data.get("ticket"),
        symbol=data.get("symbol"),
        direction=data.get("direction"),
        volume=data.get("volume"),
        open_price=data.get("open_price"),
        close_price=data.get("close_price"),
        sl=data.get("sl"),
        tp=data.get("tp"),
        profit=data.get("profit"),
        event_timestamp=event_ts,
        message=data.get("message"),
        raw=data,
    )
    try:
        async with get_session() as session:
            session.add(row)
        log.debug(
            "trade_event written: type=%s subscriber=%s ticket=%s",
            row.event_type,
            row.subscriber_id,
            row.ticket,
        )
    except Exception as exc:
        log.error("Failed to write trade_event: %s", exc)
