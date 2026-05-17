"""
broker/services/publisher_service.py — NATS publisher that broadcasts trading signals to all subscribers.

Security
--------
Authentication is handled by the NATS server via token auth (``settings.NATS_TOKEN``).
All traffic can be encrypted end-to-end by enabling TLS on the NATS server.

Workers subscribe to subjects ``SIGNAL`` and ``ADMIN``.
Message body is a raw JSON string (no topic prefix).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

import nats
from nats.aio.client import Client as NATSClient

from broker.schemas.publisher_schema import PublishTopicEnum, TradingSignal
from broker.services.notification_service import TelegramNotification
from broker.settings import settings
from broker.logger import get_logger

log = get_logger(__name__)


class NatsPublisher:
  """
  Async NATS publisher for broadcasting trading signals to subscriber nodes.

  Each signal is published to a subject of the form ``PublishTopicEnum``
  (e.g. ``SIGNAL``).  Workers subscribe to ``SIGNAL|ADMIN`` or a
  specific subject.  Message body is a raw JSON string.
  """

  PUBLISH_SUBJECTS = [PublishTopicEnum.SIGNAL, PublishTopicEnum.ADMIN]

  def __init__(self) -> None:
    self._nc: Optional[NATSClient] = None

  def _subjects_line(self) -> str:
    return " | ".join(s.value for s in self.PUBLISH_SUBJECTS)

  async def connect(self) -> None:
    """Connect to the NATS server and register lifecycle callbacks."""
    opts: dict = {
      "servers": [settings.nats_url],
      "disconnected_cb": self._on_disconnected,
      "reconnected_cb": self._on_reconnected,
      "closed_cb": self._on_closed,
      "error_cb": self._on_error,
    }
    if settings.NATS_TOKEN:
      opts["token"] = settings.NATS_TOKEN

    self._nc = await nats.connect(**opts)
    log.info("NATS publisher connected to %s", settings.nats_url)

  # ── Public API ────────────────────────────────────────────────────

  async def publish(
    self,
    subject: PublishTopicEnum = PublishTopicEnum.SIGNAL,
    signal: TradingSignal = None,
  ) -> None:
    """Serialise *signal* and broadcast to all connected subscribers."""
    if signal is None:
      return
    payload = signal.model_dump_json().encode()
    await self._nc.publish(subject.value, payload)
    log.info(
      "Published [%s] signal_id=%s action=%s symbol=%s",
      subject.value,
      signal.signal_id,
      signal.action,
      signal.symbol,
    )

  async def publish_flat(self, symbol: str, timestamp: datetime, strategy: str) -> None:
    """Broadcast a FLAT (close-all) directive to all connected subscribers."""
    payload = json.dumps(
      {
        "strategy": strategy,
        "timestamp": timestamp.isoformat(),
        "action": "FLAT",
        "symbol": symbol,
      }
    ).encode()
    await self._nc.publish(PublishTopicEnum.SIGNAL.value, payload)
    log.info(
      "Published [%s] FLAT directive symbol=%s", PublishTopicEnum.SIGNAL.value, symbol
    )

  async def close(self) -> None:
    """Gracefully drain pending messages and close the NATS connection."""
    if self._nc and not self._nc.is_closed:
      await self._nc.drain()
      await self._nc.close()
    log.info("NATS publisher closed.")

  # ── Lifecycle callbacks ───────────────────────────────────────────

  async def _on_disconnected(self) -> None:
    log.warning("NATS publisher disconnected")
    TelegramNotification().send_message(
      f"🔴 <b>NATS Publisher Disconnected</b>\n"
      f"📤 Publishing: <code>{self._subjects_line()}</code>"
    )

  async def _on_reconnected(self) -> None:
    log.info("NATS publisher reconnected to %s", settings.nats_url)
    TelegramNotification().send_message(
      f"🔌 <b>NATS Publisher Reconnected</b>\n"
      f"📤 Publishing: <code>{self._subjects_line()}</code>"
    )

  async def _on_closed(self) -> None:
    log.info("NATS publisher connection closed")

  async def _on_error(self, exc: Exception) -> None:
    log.error("NATS publisher error: %s", exc)
