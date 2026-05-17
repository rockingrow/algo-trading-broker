"""
broker/services/trade_listener.py
─────────────────────────────────
Async NATS subscriber for the TRADE subject. Worker nodes publish a
PositionEvent here every time their SQLite `positions` table row is inserted
or updated; this listener applies the event to the broker's PostgreSQL
`trades` table.

Runs inside FastAPI's asyncio event loop — no separate thread is needed since
the broker is fully async (unlike the worker, which has to bridge a threaded
MT5 child process).
"""

from __future__ import annotations

import json
from typing import Optional

import nats
from nats.aio.client import Client as NATSClient
from nats.aio.subscription import Subscription
from pydantic import ValidationError

from broker.db.repository import upsert_trade_by_position_event
from broker.logger import get_logger
from broker.schemas.publisher_schema import PublishTopicEnum
from broker.schemas.trade_event_schema import PositionEvent
from broker.services.notification_service import TelegramNotification
from broker.settings import settings

log = get_logger(__name__)


class NatsTradeListener:
  """Async NATS subscriber for the TRADE subject."""

  def __init__(self) -> None:
    self._nc: Optional[NATSClient] = None
    self._sub: Optional[Subscription] = None

  LISTEN_SUBJECT = PublishTopicEnum.TRADE

  async def start(self) -> None:
    opts: dict = {
      "servers": [settings.nats_url],
      "max_reconnect_attempts": -1,
      "reconnect_time_wait": 5,
      "disconnected_cb": self._on_disconnected,
      "reconnected_cb": self._on_reconnected,
      "error_cb": self._on_error,
    }
    if settings.NATS_TOKEN:
      opts["token"] = settings.NATS_TOKEN

    self._nc = await nats.connect(**opts)
    self._sub = await self._nc.subscribe(self.LISTEN_SUBJECT.value, cb=self._handle)
    log.info(
      "NATS trade listener subscribed to '%s' on %s",
      self.LISTEN_SUBJECT.value,
      settings.nats_url,
    )

  async def stop(self) -> None:
    if self._sub is not None:
      try:
        await self._sub.unsubscribe()
      except Exception as exc:
        log.warning("Failed to unsubscribe TRADE listener: %s", exc)
    if self._nc is not None and not self._nc.is_closed:
      await self._nc.drain()
      await self._nc.close()
    log.info("NATS trade listener stopped.")

  async def _handle(self, msg) -> None:
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

  async def _on_disconnected(self) -> None:
    log.warning("NATS trade listener disconnected")
    TelegramNotification().send_message(
      f"🔴 <b>NATS Listener Disconnected</b>\n"
      f"📥 Listening: <code>{self.LISTEN_SUBJECT.value}</code>"
    )

  async def _on_reconnected(self) -> None:
    log.info("NATS trade listener reconnected to %s", settings.nats_url)
    TelegramNotification().send_message(
      f"🔌 <b>NATS Listener Reconnected</b>\n"
      f"📥 Listening: <code>{self.LISTEN_SUBJECT.value}</code>"
    )

  async def _on_error(self, exc: Exception) -> None:
    log.error("NATS trade listener error: %s", exc)
