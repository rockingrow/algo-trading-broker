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

# Window (seconds) in which JetStream rejects a second message carrying a
# ``Nats-Msg-Id`` it has already stored. The webhook path retries an enqueue whose
# PubAck did not arrive in time, and that ack may simply have been slow rather
# than lost — without a dedup window the retry would store the same alert
# twice and workers would open two positions. nats-py sends
# ``duplicate_window: 0`` (dedup off) unless it is set explicitly, so it is
# always spelled out here.
JETSTREAM_DUPLICATE_WINDOW_SECONDS = 120.0


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

  @property
  def is_connected(self) -> bool:
    """True only while the client holds a live connection to a NATS server.

    Publishing while the client is reconnecting does not fail fast — the write
    is buffered and a JetStream ack simply never arrives, so the caller waits
    out its whole timeout. Callers on a latency budget (the webhook) check this
    first and take their fallback path immediately instead.
    """
    return self._nc is not None and self._nc.is_connected

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
    if settings.nats.TOKEN:
      opts["token"] = settings.nats.TOKEN

    self._nc = await nats_lib.connect(**opts)
    log.info("NATS connected to %s", settings.nats_url)

    await self.ensure_signal_stream()

  async def ensure_signal_stream(self) -> None:
    """Idempotently create the JetStream stream that backs webhook events.

    The webhook endpoint must succeed as long as JetStream itself is reachable,
    so the stream has to exist before the first ``publish`` call. Calling
    ``add_stream`` on an existing stream is a no-op when the config matches; a
    mismatch (an older stream created before ``duplicate_window`` was set, or
    someone tweaking retention/storage out-of-band) is reconciled with
    ``update_stream`` so a deployment that predates a config change does not
    silently keep running on the old one.
    """
    config = StreamConfig(
      name=JETSTREAM_SIGNAL_STREAM,
      subjects=[JETSTREAM_SIGNAL_SUBJECT_FILTER],
      retention=RetentionPolicy.WORK_QUEUE,
      storage=StorageType.FILE,
      max_msgs=-1,
      max_bytes=-1,
      duplicate_window=JETSTREAM_DUPLICATE_WINDOW_SECONDS,
    )
    try:
      await self.js.add_stream(config=config)
      log.info(
        "JetStream stream ensured: %s (subjects=%s, duplicate_window=%.0fs)",
        JETSTREAM_SIGNAL_STREAM,
        JETSTREAM_SIGNAL_SUBJECT_FILTER,
        JETSTREAM_DUPLICATE_WINDOW_SECONDS,
      )
      return
    except BadRequestError as exc:
      log.info(
        "JetStream stream '%s' exists with a different config (%s) — updating it",
        JETSTREAM_SIGNAL_STREAM,
        exc,
      )

    try:
      await self.js.update_stream(config=config)
      log.info(
        "JetStream stream '%s' reconciled (duplicate_window=%.0fs)",
        JETSTREAM_SIGNAL_STREAM,
        JETSTREAM_DUPLICATE_WINDOW_SECONDS,
      )
    except Exception as exc:
      # Not every field can be updated in place (storage type, for one). Keep
      # running on the existing stream rather than refusing to start — the
      # webhook still works, it just does not get this config's guarantees.
      log.warning(
        "Failed to reconcile JetStream stream '%s': %s. "
        "Enqueue de-duplication may be inactive.",
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
