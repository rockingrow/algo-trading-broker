"""
broker/nats.py — Core NATS connection manager and module-level singleton.

Owns the raw NATSClient lifecycle: connect, drain, close, and the
disconnected/reconnected/error callbacks. It depends only on the ``Notifier``
abstraction for lifecycle alerts (injected via ``set_notifier``), not on any
concrete channel. Domain logic (publish, subscribe) lives in
``broker/services/nats_service.py``.
"""

from __future__ import annotations

from typing import Optional

import nats as nats_lib
from nats.aio.client import Client as NATSClient
from nats.js import JetStreamContext
from nats.js.api import RetentionPolicy, StorageType, StreamConfig
from nats.js.errors import BadRequestError

from broker.helpers import emoji_constants as em
from broker.interfaces import Notifier
from broker.logger import get_logger
from broker.schemas.publisher_schema import PublishTopicEnum
from broker.settings import settings

log = get_logger(__name__)

# JetStream stream that holds the durable webhook-event log the broker consumes
# in the background. Naming is deliberately generic — one stream per subject
# prefix; ``JETSTREAM_SIGNAL_SUBJECT_PREFIX`` gives us per-strategy topic
# partitioning under the stream (SIGNALS.<strategy>) without needing more.
JETSTREAM_SIGNAL_STREAM = "SIGNALS"
JETSTREAM_SIGNAL_SUBJECT_PREFIX = "SIGNALS"
JETSTREAM_SIGNAL_SUBJECT_FILTER = "SIGNALS.>"


class NatsClient:
  """Manages the NATS connection and its lifecycle callbacks."""

  PUBLISH_SUBJECTS = [PublishTopicEnum.ADMIN, PublishTopicEnum.SYSTEM]
  LISTEN_SUBJECT = PublishTopicEnum.TRADE
  LISTEN_SUBJECTS = [PublishTopicEnum.TRADE, PublishTopicEnum.SYSTEM]

  def __init__(self, notifier: Optional[Notifier] = None) -> None:
    self._nc: Optional[NATSClient] = None
    self._js: Optional[JetStreamContext] = None
    self._notifier: Optional[Notifier] = notifier

  @property
  def nc(self) -> NATSClient:
    return self._nc

  @property
  def js(self) -> JetStreamContext:
    """JetStream context bound to the current NATS connection.

    Created lazily on the first access after ``connect()`` so callers that only
    need core NATS never pay for the extra request/reply handshake.
    """
    if self._js is None:
      if self._nc is None:
        raise RuntimeError("NATS connection not established — call connect() first.")
      self._js = self._nc.jetstream()
    return self._js

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

    await self.ensure_signal_stream()

  async def ensure_signal_stream(self) -> None:
    """Idempotently create the JetStream stream that backs webhook events.

    The webhook endpoint must succeed as long as JetStream itself is reachable,
    so the stream has to exist before the first ``publish`` call. Calling
    ``add_stream`` on an existing stream is a no-op when the config matches; a
    mismatch (someone tweaked retention/storage out-of-band) is logged so it
    can be reconciled instead of silently swallowed.
    """
    config = StreamConfig(
      name=JETSTREAM_SIGNAL_STREAM,
      subjects=[JETSTREAM_SIGNAL_SUBJECT_FILTER],
      retention=RetentionPolicy.WORK_QUEUE,
      storage=StorageType.FILE,
      max_msgs=-1,
      max_bytes=-1,
    )
    try:
      await self.js.add_stream(config=config)
      log.info(
        "JetStream stream ensured: %s (subjects=%s)",
        JETSTREAM_SIGNAL_STREAM,
        JETSTREAM_SIGNAL_SUBJECT_FILTER,
      )
    except BadRequestError as exc:
      log.warning(
        "JetStream stream '%s' already exists with a different config: %s",
        JETSTREAM_SIGNAL_STREAM,
        exc,
      )

  async def close(self) -> None:
    """Drain pending messages and close the connection."""
    if self._nc is not None and not self._nc.is_closed:
      await self._nc.drain()
      await self._nc.close()
    self._js = None
    log.info("NATS connection closed.")

  # ── Lifecycle callbacks ───────────────────────────────────────────

  async def _on_disconnected(self) -> None:
    log.warning("NATS disconnected")
    await self._notify(
      f"{em.NATS_DISCONNECTED} <b>NATS Disconnected</b>\n"
      f"{em.PUBLISH} Publishing: <code>{self.subjects_line()}</code> + dynamic (by strategy & per-account ADMIN)\n"
      f"{em.LISTEN} Listening: <code>{self.listen_subjects_line()}</code>"
    )

  async def _on_reconnected(self) -> None:
    log.info("NATS reconnected to %s", settings.nats_url)
    await self._notify(
      f"{em.NATS_RECONNECTED} <b>NATS Reconnected</b>\n"
      f"{em.PUBLISH} Publishing: <code>{self.subjects_line()}</code> + dynamic (by strategy & per-account ADMIN)\n"
      f"{em.LISTEN} Listening: <code>{self.listen_subjects_line()}</code>"
    )

  async def _on_closed(self) -> None:
    log.info("NATS connection closed")

  async def _on_error(self, exc: Exception) -> None:
    log.error("NATS error: %s", exc)


nats_client = NatsClient()
