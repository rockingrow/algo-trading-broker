"""
broker/services/telegram_log_handler.py — Forward ERROR-level logs to Telegram.

A standard :class:`logging.Handler` cannot ``await`` anything: ``emit`` is
synchronous and may run from any context (sync code, the event loop, a worker
thread). Yet :class:`~broker.services.notification_service.TelegramNotification`
sends over the network with ``httpx`` and must be awaited.

This module bridges the two with a queue + background worker:

* ``emit`` only formats the record and hands it to the event loop via
  ``loop.call_soon_threadsafe`` — it never blocks and never raises.
* A long-lived worker task (started during the app lifespan) drains the queue
  and performs the actual async send.

Two safeguards keep this from misbehaving in production:

* **No recursion.** ``notification_service`` logs an error when a Telegram send
  fails. A filter drops records originating from that module (and this one), so
  a failing send can never trigger another send.
* **No spam.** Identical messages are suppressed within a short dedup window and
  the queue is bounded, dropping records when saturated rather than growing
  without limit.
"""

from __future__ import annotations

import asyncio
import logging
import time

from broker.helpers import emoji_constants as em
from broker.settings import settings

# Loggers whose records must never be forwarded, to avoid an infinite
# send → fail → log error → send loop.
_EXCLUDED_PREFIXES = (
  "broker.services.notification_service",
  "broker.services.telegram_log_handler",
)

_QUEUE_MAXSIZE = 100


class _RecursionFilter(logging.Filter):
  """Drop records emitted by the Telegram send path itself."""

  def filter(self, record: logging.LogRecord) -> bool:
    return not record.name.startswith(_EXCLUDED_PREFIXES)


class TelegramLogHandler(logging.Handler):
  """Logging handler that forwards ERROR+ records to the management chat."""

  def __init__(self) -> None:
    super().__init__(level=logging.ERROR)
    self.addFilter(_RecursionFilter())
    self.setFormatter(
      logging.Formatter(
        fmt="%(levelname)s | %(name)s\n%(message)s",
      )
    )
    self._loop: asyncio.AbstractEventLoop | None = None
    self._queue: asyncio.Queue[str] | None = None
    self._task: asyncio.Task[None] | None = None
    # message -> monotonic timestamp of last forward, for dedup.
    self._recent: dict[str, float] = {}

  # ── lifecycle (called from the app lifespan) ─────────────────────
  def start(self, loop: asyncio.AbstractEventLoop) -> None:
    """Bind the running event loop and launch the background worker."""
    self._loop = loop
    if self._queue is None:
      self._queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
    if self._task is None or self._task.done():
      self._task = loop.create_task(self._worker())

  async def stop(self) -> None:
    """Cancel the worker task cleanly."""
    if self._task is not None:
      self._task.cancel()
      try:
        await self._task
      except asyncio.CancelledError:
        pass
      self._task = None

  # ── logging.Handler API ──────────────────────────────────────────
  def emit(self, record: logging.LogRecord) -> None:
    try:
      loop = self._loop
      if loop is None or loop.is_closed():
        return  # not started yet (or shut down) — nothing to forward to

      message = self.format(record)
      if self._is_duplicate(message):
        return

      loop.call_soon_threadsafe(self._enqueue, message)
    except Exception:  # pragma: no cover — handlers must never raise
      self.handleError(record)

  # ── internals ────────────────────────────────────────────────────
  def _is_duplicate(self, message: str) -> bool:
    """Return True if *message* was forwarded within the dedup window."""
    window = settings.TELEGRAM_LOG_DEDUP_WINDOW
    now = time.monotonic()
    # Prune stale entries so the dict cannot grow unbounded.
    self._recent = {
      msg: ts for msg, ts in self._recent.items() if now - ts < window
    }
    if message in self._recent:
      return True
    self._recent[message] = now
    return False

  def _enqueue(self, message: str) -> None:
    """Push onto the queue from within the event loop thread; drop if full."""
    if self._queue is None:
      return
    try:
      self._queue.put_nowait(message)
    except asyncio.QueueFull:
      pass  # under an error storm, dropping is preferable to blocking

  async def _worker(self) -> None:
    # Imported lazily to keep module import side-effect free and avoid any
    # import-order coupling with the logging bootstrap.
    from broker.services.notification_service import TelegramNotification

    assert self._queue is not None
    notifier = TelegramNotification(chat_id=settings.TELEGRAM_CHAT_ID)
    while True:
      message = await self._queue.get()
      try:
        await notifier.send_message(f"{em.ERROR_ALERT} {message}")
      except Exception:
        # Never surface failures back through logging (would risk recursion)
        # and never let the worker die.
        pass
      finally:
        self._queue.task_done()


# Shared singleton: every logger forwards through one queue/worker.
telegram_log_handler = TelegramLogHandler()
