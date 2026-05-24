"""
broker/nats.py — Core NATS connection manager and module-level singleton.

Owns the raw NATSClient lifecycle: connect, drain, close, and the
disconnected/reconnected/error callbacks.  Domain logic (publish, subscribe)
lives in broker/services/nats_service.py which imports ``nats_client`` from
here.
"""

from __future__ import annotations

from typing import Optional

import nats as nats_lib
from nats.aio.client import Client as NATSClient

from broker.logger import get_logger
from broker.schemas.publisher_schema import PublishTopicEnum
from broker.services.notification_service import TelegramNotification
from broker.settings import settings

log = get_logger(__name__)


class NatsClient:
  """Manages the NATS connection and its lifecycle callbacks."""

  PUBLISH_SUBJECTS = [PublishTopicEnum.ADMIN]
  LISTEN_SUBJECT = PublishTopicEnum.TRADE

  def __init__(self) -> None:
    self._nc: Optional[NATSClient] = None

  @property
  def nc(self) -> NATSClient:
    return self._nc

  def _subjects_line(self) -> str:
    return " | ".join(s.value for s in self.PUBLISH_SUBJECTS)

  async def connect(self) -> None:
    """Establish connection to the NATS server."""
    opts: dict = {
      "servers": [settings.nats_url],
      "max_reconnect_attempts": -1,
      "reconnect_time_wait": 5,
      "disconnected_cb": self._on_disconnected,
      "reconnected_cb": self._on_reconnected,
      "closed_cb": self._on_closed,
      "error_cb": self._on_error,
    }
    if settings.NATS_TOKEN:
      opts["token"] = settings.NATS_TOKEN

    self._nc = await nats_lib.connect(**opts)
    log.info("NATS connected to %s", settings.nats_url)

  async def close(self) -> None:
    """Drain pending messages and close the connection."""
    if self._nc is not None and not self._nc.is_closed:
      await self._nc.drain()
      await self._nc.close()
    log.info("NATS connection closed.")

  # ── Lifecycle callbacks ───────────────────────────────────────────

  async def _on_disconnected(self) -> None:
    log.warning("NATS disconnected")
    TelegramNotification().send_message(
      "🔴 <b>NATS Disconnected</b>\n"
      f"📤 Publishing: <code>{self._subjects_line()}</code> + dynamic (by strategy)\n"
      f"📥 Listening: <code>{self.LISTEN_SUBJECT.value}</code>"
    )

  async def _on_reconnected(self) -> None:
    log.info("NATS reconnected to %s", settings.nats_url)
    TelegramNotification().send_message(
      "🔌 <b>NATS Reconnected</b>\n"
      f"📤 Publishing: <code>{self._subjects_line()}</code> + dynamic (by strategy)\n"
      f"📥 Listening: <code>{self.LISTEN_SUBJECT.value}</code>"
    )

  async def _on_closed(self) -> None:
    log.info("NATS connection closed")

  async def _on_error(self, exc: Exception) -> None:
    log.error("NATS error: %s", exc)


nats_client = NatsClient()
