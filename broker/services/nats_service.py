"""
broker/services/nats_service.py — NATS interface layer for trading operations.

Reuses the ``nats_client`` singleton from ``broker/nats.py``.
Exposes publish/publish_flat for other services and handles inbound trade events.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from nats.aio.subscription import Subscription
from pydantic import ValidationError

from broker.db.repository import upsert_trade_by_position_event
from broker.logger import get_logger
from broker.nats import nats_client
from broker.schemas.publisher_schema import TradingSignal
from broker.schemas.trade_event_schema import PositionEvent

log = get_logger(__name__)


class NatsService:
  """Interface layer: publish trading signals and consume trade events."""

  def __init__(self) -> None:
    self._sub: Optional[Subscription] = None

  async def start(self) -> None:
    """Subscribe to the TRADE subject using the shared NATS connection."""
    self._sub = await nats_client.nc.subscribe(
      nats_client.LISTEN_SUBJECT.value, cb=self.handle_subject_trade
    )
    log.info("NATS trade listener subscribed to '%s'", nats_client.LISTEN_SUBJECT.value)

  async def stop(self) -> None:
    """Unsubscribe from the TRADE subject."""
    if self._sub is not None:
      try:
        await self._sub.unsubscribe()
      except Exception as exc:
        log.warning("Failed to unsubscribe TRADE listener: %s", exc)
    log.info("NATS trade listener stopped.")

  # ── Publisher interface ───────────────────────────────────────────

  async def publish(self, signal: TradingSignal) -> None:
    """Serialise *signal* and broadcast to subscribers on the strategy subject."""
    if signal is None:
      return
    subject = signal.strategy
    payload = signal.model_dump_json().encode()
    await nats_client.nc.publish(subject, payload)
    log.info(
      "Published [%s] signal_id=%s action=%s symbol=%s",
      subject,
      signal.signal_id,
      signal.action,
      signal.symbol,
    )

  async def publish_flat(self, symbol: str, timestamp: datetime, strategy: str) -> None:
    """Broadcast a FLAT (close-all) directive on the strategy subject."""
    payload = json.dumps(
      {
        "strategy": strategy,
        "timestamp": timestamp.isoformat(),
        "action": "FLAT",
        "symbol": symbol,
      }
    ).encode()
    await nats_client.nc.publish(strategy, payload)
    log.info("Published [%s] FLAT directive symbol=%s", strategy, symbol)

  async def handle_subject_trade(self, msg) -> None:
    """Handle incoming TRADE events from the NATS subject."""
    raw = msg.data.decode()
    try:
      data = json.loads(raw)
      event = PositionEvent(**data)
    except json.JSONDecodeError as exc:
      log.error("TRADE listener: malformed JSON: %s | raw=%s", exc, raw)
      return
    except ValidationError as exc:
      log.error("TRADE listener: invalid PositionEvent: %s | raw=%s", exc, raw)
      return

    log.info(
      "TRADE event=%s account_id=%s source_ticket=%s status=%s",
      event.event,
      event.account_id,
      event.source_ticket,
      event.status,
    )
    try:
      await upsert_trade_by_position_event(event)
    except Exception as exc:
      log.exception("Failed to apply TRADE event: %s", exc)
