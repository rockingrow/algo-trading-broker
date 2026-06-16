"""
broker/services/nats_consumer.py — Inbound side of NATS.

Subscribes to the TRADE subject and applies each position event to the trades
table via an injected ``TradeRepository``. It depends on the repository
abstraction, not on a concrete persistence function.
"""

from __future__ import annotations

import json
from typing import Optional

from nats.aio.subscription import Subscription
from pydantic import ValidationError

from broker.interfaces import TradeRepository
from broker.logger import get_logger
from broker.nats import NatsClient, nats_client
from broker.schemas.trade_event_schema import PositionEvent

log = get_logger(__name__)


class TradeEventConsumer:
  """Consumes TRADE events from NATS and persists them via a TradeRepository."""

  def __init__(
    self,
    trade_repository: TradeRepository,
    connection: NatsClient | None = None,
  ) -> None:
    self._repo = trade_repository
    self._conn = connection or nats_client
    self._sub: Optional[Subscription] = None

  async def start(self) -> None:
    """Subscribe to the TRADE subject using the shared NATS connection."""
    self._sub = await self._conn.nc.subscribe(
      self._conn.LISTEN_SUBJECT.value, cb=self.handle_subject_trade
    )
    log.info("NATS trade listener subscribed to '%s'", self._conn.LISTEN_SUBJECT.value)

  async def stop(self) -> None:
    """Unsubscribe from the TRADE subject."""
    if self._sub is not None:
      try:
        await self._sub.unsubscribe()
      except Exception as exc:
        log.warning("Failed to unsubscribe TRADE listener: %s", exc)
    log.info("NATS trade listener stopped.")

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
      "TRADE event=%s account_id=%s ref_id=%s status=%s",
      event.event,
      event.account_id,
      event.ref_source_id,
      event.status,
    )
    try:
      await self._repo.upsert_by_position_event(event)
    except Exception as exc:
      log.exception("Failed to apply TRADE event: %s", exc)
