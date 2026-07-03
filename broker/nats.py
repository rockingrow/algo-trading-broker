"""
broker/nats.py — Core NATS connection manager and module-level singleton.

Owns the raw NATSClient lifecycle: connect, drain, close, and the
disconnected/reconnected/error callbacks. It depends only on the ``Notifier``
abstraction for lifecycle alerts (injected via ``set_notifier``), not on any
concrete channel. Domain logic (publish, subscribe) lives in
``broker/services/nats_publisher.py`` and ``broker/services/nats_service.py``.
"""

from __future__ import annotations

from typing import Optional

import nats as nats_lib
from nats.aio.client import Client as NATSClient

from broker.helpers import emoji_constants as em
from broker.interfaces import Notifier
from broker.logger import get_logger
from broker.schemas.publisher_schema import PublishTopicEnum
from broker.settings import settings

log = get_logger(__name__)


class NatsClient:
  """Manages the NATS connection and its lifecycle callbacks."""

  PUBLISH_SUBJECTS = [PublishTopicEnum.ADMIN, PublishTopicEnum.SYSTEM]
  LISTEN_SUBJECT = PublishTopicEnum.TRADE
  LISTEN_SUBJECTS = [PublishTopicEnum.TRADE, PublishTopicEnum.SYSTEM]

  def __init__(self, notifier: Optional[Notifier] = None) -> None:
    self._nc: Optional[NATSClient] = None
    self._notifier: Optional[Notifier] = notifier

  @property
  def nc(self) -> NATSClient:
    return self._nc

  def set_notifier(self, notifier: Notifier) -> None:
    """Wire a notification channel used for connection lifecycle alerts."""
    self._notifier = notifier

  def subjects_line(self) -> str:
    return " | ".join(s.value for s in self.PUBLISH_SUBJECTS)

  def listen_subjects_line(self) -> str:
    return " | ".join(s.value for s in self.LISTEN_SUBJECTS)

  async def _notify(self, message: str) -> None:
    if self._notifier is not None:
      await self._notifier.send_message(message)

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
    await self._notify(
      f"{em.NATS_DISCONNECTED} <b>NATS Disconnected</b>\n"
      f"{em.PUBLISH} Publishing: <code>{self.subjects_line()}</code> + dynamic (by strategy)\n"
      f"{em.LISTEN} Listening: <code>{self.listen_subjects_line()}</code>"
    )

  async def _on_reconnected(self) -> None:
    log.info("NATS reconnected to %s", settings.nats_url)
    await self._notify(
      f"{em.NATS_RECONNECTED} <b>NATS Reconnected</b>\n"
      f"{em.PUBLISH} Publishing: <code>{self.subjects_line()}</code> + dynamic (by strategy)\n"
      f"{em.LISTEN} Listening: <code>{self.listen_subjects_line()}</code>"
    )

  async def _on_closed(self) -> None:
    log.info("NATS connection closed")

  async def _on_error(self, exc: Exception) -> None:
    log.error("NATS error: %s", exc)


nats_client = NatsClient()
