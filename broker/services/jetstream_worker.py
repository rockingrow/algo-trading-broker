"""
broker/services/jetstream_worker.py — the pull-consumer loop every durable
JetStream subscription of the broker shares.

``SignalWorker`` (stream ``SIGNALS``) and ``TradeEventConsumer`` (stream
``TRADES``) used to differ only in what they do with one message; the fetch
loop, its idle/backoff handling and the ack/NAK/term bookkeeping around it are
the same. ``JetStreamPullWorker`` owns that loop (a template method) and a
subclass supplies ``_handle_one`` — deserialise, process, then settle the
message with ``_ack``, ``_nak`` or ``_term``.

The settle helpers never raise: a failed ack is logged and JetStream simply
redelivers after ``ack_wait``, which every handler here is written to survive.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from nats.js.errors import BadRequestError

from broker.logger import get_logger
from broker.nats import NatsClient, nats_client
from broker.settings import settings

log = get_logger(__name__)

# Seconds JetStream waits before redelivering a message a handler NAK-ed.
# Without a delay a NAK is redelivered at once, so a database outage turned
# every queued message into a tight loop of failed writes and stack traces.
NAK_DELAY_SECONDS = 5.0


def delivery_count(msg) -> int:
  """How many times JetStream has delivered *msg* — 1 when it carries no
  metadata (a hand-built message in a test)."""
  try:
    return int(msg.metadata.num_delivered)
  except Exception:
    return 1


class JetStreamPullWorker:
  """Fetches from a durable pull consumer and hands each message to a subclass."""

  #: Shown in log lines and used as the asyncio task name.
  worker_name = "jetstream-worker"

  def __init__(
    self,
    *,
    connection: NatsClient | None,
    stream: str,
    subject: str,
    durable: str,
  ) -> None:
    self._conn = connection or nats_client
    self._stream = stream
    self._subject = subject
    self._durable = durable
    self._task: Optional[asyncio.Task] = None
    self._stop = asyncio.Event()
    self._sub = None

  # ── lifecycle ──────────────────────────────────────────────────────

  async def start(self) -> None:
    """Create the durable pull consumer and launch the background fetch loop."""
    try:
      self._sub = await self._conn.js.pull_subscribe(
        subject=self._subject,
        durable=self._durable,
        stream=self._stream,
      )
    except BadRequestError as exc:
      log.error("Failed to create JetStream pull consumer '%s': %s", self._durable, exc)
      raise

    self._stop.clear()
    self._task = asyncio.create_task(self._run(), name=self.worker_name)
    log.info(
      "%s started stream=%s consumer=%s subject=%s",
      self.worker_name,
      self._stream,
      self._durable,
      self._subject,
    )

  async def stop(self) -> None:
    """Stop the fetch loop and wait for the current message to finish."""
    self._stop.set()
    if self._task is not None:
      try:
        await self._task
      except asyncio.CancelledError:
        pass
      self._task = None
    if self._sub is not None:
      try:
        await self._sub.unsubscribe()
      except Exception as exc:
        log.warning("Failed to unsubscribe %s: %s", self.worker_name, exc)
      self._sub = None
    log.info("%s stopped.", self.worker_name)

  # ── loop ───────────────────────────────────────────────────────────

  async def _run(self) -> None:
    while not self._stop.is_set():
      try:
        msgs = await self._sub.fetch(
          settings.jetstream.FETCH_BATCH,
          timeout=settings.jetstream.FETCH_TIMEOUT_SECONDS,
        )
      except TimeoutError:
        # An empty fetch window is the normal idle case, not an error. Catch the
        # built-in base class: nats-py re-raises a bare ``asyncio.TimeoutError``
        # (which *is* ``builtins.TimeoutError`` since 3.11) when the window
        # expires before any message or status reply arrives, and only wraps it
        # as ``FetchTimeoutError`` once the server has answered. The base also
        # covers ``nats.errors.TimeoutError`` and ``FetchTimeoutError``, both of
        # which derive from it.
        continue
      except asyncio.CancelledError:
        raise
      except Exception as exc:
        log.exception("JetStream fetch failed: %s", exc)
        # Back off briefly on unexpected errors so we don't spin at 100% CPU
        # if the connection is in a broken state; ``stop`` breaks out early.
        await asyncio.sleep(1.0)
        continue

      for msg in msgs:
        try:
          await self._handle_one(msg)
        except asyncio.CancelledError:
          raise
        except Exception as exc:
          # A handler is meant to settle every message itself; one that slips
          # through must not end the loop — every later message would sit in
          # the stream with nothing pulling it. NAK-ed, so it is redelivered.
          log.exception("%s: unhandled error on a message: %s", self.worker_name, exc)
          await self._nak(msg)

  async def _handle_one(self, msg) -> None:
    """Process one message and settle it. Supplied by the subclass."""
    raise NotImplementedError

  # ── settling ───────────────────────────────────────────────────────

  async def _ack(self, msg) -> None:
    try:
      await msg.ack()
    except Exception as exc:
      log.warning("Failed to ack JetStream message: %s", exc)

  async def _nak(self, msg) -> None:
    try:
      await msg.nak(delay=NAK_DELAY_SECONDS)
    except Exception as exc:
      log.warning("Failed to nak JetStream message: %s", exc)

  async def _in_progress(self, msg) -> None:
    """Tell JetStream the message is still being worked on (resets ack wait)."""
    try:
      await msg.in_progress()
    except Exception as exc:
      log.debug("Failed to mark JetStream message in progress: %s", exc)

  async def _term(self, msg) -> None:
    try:
      await msg.term()
    except Exception as exc:
      log.warning("Failed to term JetStream message: %s", exc)
