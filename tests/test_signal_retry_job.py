import asyncio
import uuid
from types import SimpleNamespace

from broker.schemas.core import SignalStatusEnum
from broker.services.signal_retry_job import SignalRetryJob


class FakeSignalRepo:
  def __init__(self, rows: list[SimpleNamespace] | None = None):
    self.rows = list(rows or [])
    self.calls: list[int] = []

  async def list_retryable(self, interval_seconds: int):
    self.calls.append(interval_seconds)
    return list(self.rows)

  async def log_signal(self, payload):
    return "sig-id"

  async def mark_published(self, signal_id):
    return True

  async def get_by_id(self, signal_id):
    return None

  async def record_attempt_failure(self, signal_id):
    return None

  async def list_recent_by_strategies(self, strategies, since_seconds):
    return []


class FakeService:
  def __init__(self, raise_exc: Exception | None = None):
    self.retried: list[str] = []
    self._raise = raise_exc

  async def retry_signal(self, signal_id: str):
    if self._raise is not None:
      raise self._raise
    self.retried.append(signal_id)
    return {"status": "accepted", "signal_id": signal_id}


def _row(signal_id: str, attempts: int = 2) -> SimpleNamespace:
  return SimpleNamespace(
    id=uuid.UUID(signal_id),
    status=SignalStatusEnum.QUEUED,
    attempts=attempts,
    last_attempt=None,
  )


async def test_tick_forwards_each_retryable_row_to_service():
  row_a = _row("11111111-1111-1111-1111-111111111111")
  row_b = _row("22222222-2222-2222-2222-222222222222", attempts=1)
  repo = FakeSignalRepo(rows=[row_a, row_b])
  service = FakeService()
  job = SignalRetryJob(
    service=service,  # type: ignore[arg-type]
    signal_repository=repo,
    interval_seconds=15,
  )
  await job._tick_once()

  assert repo.calls == [15]
  assert service.retried == [str(row_a.id), str(row_b.id)]


async def test_tick_is_a_noop_when_nothing_is_retryable():
  service = FakeService()
  job = SignalRetryJob(
    service=service,  # type: ignore[arg-type]
    signal_repository=FakeSignalRepo(rows=[]),
    interval_seconds=15,
  )
  await job._tick_once()
  assert service.retried == []


async def test_one_bad_row_does_not_break_the_batch():
  rows = [
    _row("11111111-1111-1111-1111-111111111111"),
    _row("22222222-2222-2222-2222-222222222222"),
  ]

  class MixedService(FakeService):
    def __init__(self):
      super().__init__()
      self._first = True

    async def retry_signal(self, signal_id: str):
      if self._first:
        self._first = False
        raise RuntimeError("boom")
      self.retried.append(signal_id)
      return {"status": "accepted", "signal_id": signal_id}

  service = MixedService()
  job = SignalRetryJob(
    service=service,  # type: ignore[arg-type]
    signal_repository=FakeSignalRepo(rows=rows),
    interval_seconds=15,
  )
  await job._tick_once()
  # The first row raised; the second one still got processed.
  assert service.retried == [str(rows[1].id)]


async def test_start_stop_runs_the_loop_and_exits_cleanly():
  service = FakeService()
  repo = FakeSignalRepo(rows=[])
  # 1s interval so start()/stop() cycles quickly in the test.
  job = SignalRetryJob(
    service=service,  # type: ignore[arg-type]
    signal_repository=repo,
    interval_seconds=1,
  )
  await job.start()
  # Give the loop a chance to run at least one tick.
  await asyncio.sleep(0.05)
  await job.stop()
  assert len(repo.calls) >= 1
