"""
broker/db/listener.py — Postgres change-data-capture over LISTEN/NOTIFY.

The broadcast pipeline does not send Telegram messages at write time: a signal
(or a worker's TRADE report) commits its change plus a ``broadcast_message_logs``
row, and a trigger on that table calls ``pg_notify``. This module is the other
half — a dedicated connection that LISTENs on that channel and hands each
notification to a callback, so the dispatcher reacts to a *committed* change
rather than being called inline by whoever made it.

Why a separate raw asyncpg connection rather than the SQLAlchemy pool: a
listening session is long-lived and idle by design. Borrowing a pooled
connection for it would take that connection out of circulation for the life of
the process and break the moment the pool recycled it.

Two things keep it honest in production:

* **Reconnects.** A dropped connection silently stops delivering
  notifications, so a supervisor task re-establishes it and reports the gap.
* **It is never the only path.** ``NOTIFY`` is fire-and-forget: anything
  emitted while this is down is gone. The write log is the durable record and
  the dispatcher's sweeper replays whatever the listener missed, so a missed
  notification costs latency, never data.
"""

from __future__ import annotations

import asyncio
import json
from typing import Awaitable, Callable

import asyncpg

from broker.logger import get_logger
from broker.settings import settings

log = get_logger(__name__)

#: Channel the ``broadcast_message_logs`` trigger publishes on. Kept in sync
#: with the migration that creates ``notify_broadcast_message_log()``.
BROADCAST_LOG_CHANNEL = "broadcast_message_log"

#: How long to wait before retrying a failed connect, and how often to check
#: that the listening connection is still alive.
_RECONNECT_DELAY_SECONDS = 5.0
_HEALTH_CHECK_SECONDS = 30.0


def asyncpg_dsn() -> str:
  """The Postgres DSN without SQLAlchemy's ``+asyncpg`` driver marker.

  ``settings.postgres.dsn`` is built for SQLAlchemy; asyncpg's own connect()
  rejects that scheme, so the driver suffix is stripped here rather than
  duplicating the credential/quoting logic.
  """
  return settings.postgres.dsn.replace("postgresql+asyncpg://", "postgresql://", 1)


class PostgresChangeListener:
  """LISTENs on one Postgres channel and forwards payloads to a callback.

  The callback is awaited on the event loop, decoupled from asyncpg's own
  notification callback (which must not block or raise), via a bounded queue.
  """

  def __init__(
    self,
    channel: str,
    handler: Callable[[dict], Awaitable[None]],
    *,
    dsn: str | None = None,
    queue_maxsize: int = 1000,
  ) -> None:
    self._channel = channel
    self._handler = handler
    self._dsn = dsn or asyncpg_dsn()
    self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=queue_maxsize)
    self._conn: asyncpg.Connection | None = None
    self._tasks: list[asyncio.Task] = []
    self._stopping = asyncio.Event()

  # ── lifecycle ──────────────────────────────────────────────────────

  async def start(self) -> None:
    """Connect, subscribe, and launch the drain + supervisor tasks.

    A failed initial connect is not fatal: the supervisor keeps retrying, and
    the dispatcher's sweeper covers the gap in the meantime.
    """
    self._stopping.clear()
    await self._connect()
    self._tasks = [
      asyncio.create_task(self._drain(), name=f"pg-listener-drain-{self._channel}"),
      asyncio.create_task(
        self._supervise(), name=f"pg-listener-supervisor-{self._channel}"
      ),
    ]

  async def stop(self) -> None:
    """Stop listening and close the connection."""
    self._stopping.set()
    for task in self._tasks:
      task.cancel()
    for task in self._tasks:
      try:
        await task
      except asyncio.CancelledError:
        pass
    self._tasks = []
    await self._disconnect()
    log.info("Postgres listener stopped channel=%s", self._channel)

  @property
  def connected(self) -> bool:
    return self._conn is not None and not self._conn.is_closed()

  # ── internals ──────────────────────────────────────────────────────

  async def _connect(self) -> bool:
    try:
      self._conn = await asyncpg.connect(self._dsn)
      await self._conn.add_listener(self._channel, self._on_notify)
      log.info("Postgres listener subscribed channel=%s", self._channel)
      return True
    except Exception as exc:
      # Logged at warning, not error: the sweeper still delivers everything,
      # and an ERROR here would page for a degradation that self-heals.
      log.warning(
        "Postgres listener could not subscribe channel=%s: %s", self._channel, exc
      )
      self._conn = None
      return False

  async def _disconnect(self) -> None:
    conn, self._conn = self._conn, None
    if conn is None or conn.is_closed():
      return
    try:
      await conn.remove_listener(self._channel, self._on_notify)
    except Exception:
      pass
    try:
      await conn.close()
    except Exception as exc:
      log.warning("Failed to close Postgres listener connection: %s", exc)

  def _on_notify(self, _conn, _pid, _channel, payload: str) -> None:
    """asyncpg callback — runs on the event loop and must never block/raise."""
    try:
      data = json.loads(payload)
    except (TypeError, ValueError):
      log.warning("Postgres listener: unparseable payload: %r", payload)
      return
    if not isinstance(data, dict):
      return
    try:
      self._queue.put_nowait(data)
    except asyncio.QueueFull:
      # Dropping is safe: the sweeper re-reads whatever never got dispatched.
      log.warning("Postgres listener queue full channel=%s — dropping", self._channel)

  async def _drain(self) -> None:
    while True:
      data = await self._queue.get()
      try:
        await self._handler(data)
      except asyncio.CancelledError:
        raise
      except Exception as exc:
        log.exception("Postgres listener handler failed: %s", exc)
      finally:
        self._queue.task_done()

  async def _supervise(self) -> None:
    """Re-establish the subscription whenever the connection goes away."""
    while not self._stopping.is_set():
      try:
        await asyncio.wait_for(self._stopping.wait(), timeout=_HEALTH_CHECK_SECONDS)
        return  # stop() was called
      except asyncio.TimeoutError:
        pass

      if self.connected:
        continue
      log.warning("Postgres listener lost channel=%s — reconnecting", self._channel)
      if not await self._connect():
        await asyncio.sleep(_RECONNECT_DELAY_SECONDS)
