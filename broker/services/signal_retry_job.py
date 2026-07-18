"""
broker/services/signal_retry_job.py — periodic re-send of QUEUED signals.

The JetStream ``SignalWorker`` handles a signal exactly once from the
consumer callback. If that first fan-out fails (broker crash between publish
and mark, worker subject unreachable for a moment, …) the row is left in
``status=QUEUED`` with its ``attempts`` counter decremented. This job wakes
every ``SIGNAL_RETRY_INTERVAL_SECONDS`` and hands those still-eligible rows
back to ``SignalProcessingService.retry_signal``. When the counter hits zero
the service flips the row to ``FAILED`` on its own; this job just stops
seeing it because ``list_retryable`` filters by ``attempts > 0``.

The poll cadence and the eligibility gap are the same value on purpose: a
row that just failed cannot be re-picked until the next tick, which stops
the job from racing an in-flight attempt without needing a lock.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from broker.interfaces import SignalRepository
from broker.logger import get_logger
from broker.services.signal_processing_service import SignalProcessingService

log = get_logger(__name__)


class SignalRetryJob:
  """Background task that ticks every *interval* seconds and re-fires QUEUED signals."""

  def __init__(
    self,
    *,
    service: SignalProcessingService,
    signal_repository: SignalRepository,
    interval_seconds: int,
  ) -> None:
    self._service = service
    self._signals = signal_repository
    self._interval = interval_seconds
    self._task: Optional[asyncio.Task] = None
    self._stop = asyncio.Event()

  async def start(self) -> None:
    """Kick off the polling loop; safe to call once per app lifetime."""
    if self._task is not None:
      return
    self._stop.clear()
    self._task = asyncio.create_task(self._run(), name="signal-retry-job")
    log.info(
      "Signal retry job started (interval=%ds)", self._interval
    )

  async def stop(self) -> None:
    """Signal the loop to exit and wait for the in-flight tick to finish."""
    self._stop.set()
    if self._task is not None:
      try:
        await self._task
      except asyncio.CancelledError:
        pass
      self._task = None
    log.info("Signal retry job stopped.")

  async def _run(self) -> None:
    while not self._stop.is_set():
      try:
        await self._tick_once()
      except Exception as exc:
        # Never let one bad tick kill the loop — we want to keep polling.
        log.exception("Signal retry tick failed: %s", exc)
      # Sleep for the interval, but wake up early on stop().
      try:
        await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
      except asyncio.TimeoutError:
        pass

  async def _tick_once(self) -> None:
    """One poll cycle. Public-ish so tests can drive it deterministically."""
    rows = await self._signals.list_retryable(self._interval)
    if not rows:
      return
    log.info("Signal retry tick: %d row(s) to re-fire", len(rows))
    for row in rows:
      try:
        await self._service.retry_signal(str(row.id))
      except Exception as exc:
        # A single row's failure must not derail the rest of the batch.
        log.exception("retry_signal id=%s crashed: %s", row.id, exc)
