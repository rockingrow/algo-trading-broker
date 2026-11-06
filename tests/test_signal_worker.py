import asyncio
import json
import logging

import pytest
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js.errors import FetchTimeoutError

from broker.services.signal_processing_service import SignalWorker


class FakeMsg:
  def __init__(self, data: bytes):
    self.data = data
    self.acked = False
    self.naked = False
    self.termed = False
    self.nak_delay = None

  async def ack(self):
    self.acked = True

  async def nak(self, delay=None):
    self.naked = True
    self.nak_delay = delay

  async def term(self):
    self.termed = True


class FakeService:
  def __init__(self, raise_exc: Exception | None = None, fail_times: int | None = None):
    self.attempts: list[bool] = []
    self._fail_times = fail_times
    self.calls: list[dict] = []
    self.delivery_keys: list[str | None] = []
    self._raise = raise_exc

  async def handle_enqueued(self, *, payload, delivery_key=None, redelivered=False):
    self.attempts.append(redelivered)
    if self._raise is not None and (self._fail_times is None or self._fail_times > 0):
      if self._fail_times is not None:
        self._fail_times -= 1
      raise self._raise
    self.calls.append(payload.model_dump())
    self.delivery_keys.append(delivery_key)
    return {"status": "accepted"}


def _envelope() -> bytes:
  return json.dumps(
    {
      "payload": {
        "strategy": "wt_cross_v1",
        "symbol": "OANDA:XAUUSD",
        "timeframe": "60",
        "timestamp": "2026-06-30T00:00:00+00:00",
        "position": {"action": "LONG", "price": 100.0, "quantity": 1.0},
        "token": "secret",
      },
    }
  ).encode()


async def test_valid_envelope_is_handled_and_acked():
  service = FakeService()
  worker = SignalWorker(service=service, connection=object())  # type: ignore[arg-type]
  msg = FakeMsg(_envelope())
  await worker._handle_one(msg)

  assert len(service.calls) == 1
  assert msg.acked is True
  assert msg.naked is False
  assert msg.termed is False


async def test_malformed_json_envelope_is_termed():
  service = FakeService()
  worker = SignalWorker(service=service, connection=object())  # type: ignore[arg-type]
  msg = FakeMsg(b"{not-json")
  await worker._handle_one(msg)

  assert service.calls == []
  assert msg.termed is True
  assert msg.acked is False


async def test_envelope_missing_payload_is_termed():
  service = FakeService()
  worker = SignalWorker(service=service, connection=object())  # type: ignore[arg-type]
  msg = FakeMsg(json.dumps({"other": {}}).encode())
  await worker._handle_one(msg)

  assert msg.termed is True


async def test_invalid_webhook_payload_is_termed():
  service = FakeService()
  worker = SignalWorker(service=service, connection=object())  # type: ignore[arg-type]
  msg = FakeMsg(json.dumps({"payload": {"foo": "bar"}}).encode())
  await worker._handle_one(msg)

  assert service.calls == []
  assert msg.termed is True


async def test_handler_failure_is_naked_for_redelivery():
  service = FakeService(raise_exc=RuntimeError("boom"))
  worker = SignalWorker(service=service, connection=object(), inline_retry_delays=())  # type: ignore[arg-type]
  msg = FakeMsg(_envelope())
  await worker._handle_one(msg)

  assert msg.acked is False
  assert msg.naked is True
  assert msg.termed is False


class FakeSub:
  """A pull subscription whose ``fetch`` replays a scripted list of outcomes.

  An ``Exception`` instance in the script is raised; anything else is returned
  as the batch. Once the script runs out the worker's stop event is set, so
  ``_run`` leaves the loop instead of spinning forever.
  """

  def __init__(self, worker: SignalWorker, outcomes: list):
    self._worker = worker
    self._outcomes = list(outcomes)
    self.fetch_calls = 0

  async def fetch(self, batch: int, timeout: float | None = None):
    self.fetch_calls += 1
    if not self._outcomes:
      self._worker._stop.set()
      return []
    outcome = self._outcomes.pop(0)
    if isinstance(outcome, Exception):
      raise outcome
    return outcome


def _worker_with(outcomes: list) -> tuple[SignalWorker, FakeService, FakeSub]:
  service = FakeService()
  worker = SignalWorker(service=service, connection=object())  # type: ignore[arg-type]
  sub = FakeSub(worker, outcomes)
  worker._sub = sub
  return worker, service, sub


@pytest.mark.parametrize(
  "timeout_error",
  [
    asyncio.TimeoutError(),
    NatsTimeoutError(),
    FetchTimeoutError(),
  ],
  ids=["asyncio", "nats", "fetch"],
)
async def test_fetch_timeout_is_a_silent_idle_cycle(timeout_error, caplog):
  """Every timeout flavour nats-py can raise means "no messages", not an error.

  ``nats.js`` re-raises a bare ``asyncio.TimeoutError`` when the fetch window
  expires before the server answers at all, and that one is the *base* class of
  ``nats.errors.TimeoutError`` — not a subclass — so catching only the nats
  alias logged a traceback plus a one-second backoff on every idle poll.
  """
  msg = FakeMsg(_envelope())
  worker, service, sub = _worker_with([timeout_error, [msg]])

  with caplog.at_level(logging.ERROR):
    await worker._run()

  # The timeout cycle was skipped and the next batch still arrived.
  assert len(service.calls) == 1
  assert msg.acked is True
  assert sub.fetch_calls == 3
  assert "JetStream fetch failed" not in caplog.text


async def test_unexpected_fetch_error_is_still_logged_and_backed_off(
  caplog, monkeypatch
):
  """The quiet timeout path must not swallow a genuinely broken connection."""
  sleeps: list[float] = []

  async def fake_sleep(seconds: float) -> None:
    sleeps.append(seconds)

  monkeypatch.setattr(asyncio, "sleep", fake_sleep)
  worker, service, sub = _worker_with([ConnectionResetError("socket gone")])

  with caplog.at_level(logging.ERROR):
    await worker._run()

  assert "JetStream fetch failed" in caplog.text
  assert sleeps == [1.0]
  assert service.calls == []


# ── Delivery key and NAK delay ──────────────────────────────────────


async def test_delivery_key_is_the_stream_sequence_and_store_time():
  from datetime import datetime, timezone
  from types import SimpleNamespace

  from broker.services.signal_processing_service import delivery_key_for

  msg = FakeMsg(_envelope())
  # A producer-chosen Nats-Msg-Id is ignored: JetStream only de-duplicates it
  # inside its window, so a reused id must not alias an older signal.
  msg.headers = {"Nats-Msg-Id": "reused-id"}
  msg.metadata = SimpleNamespace(
    stream="SIGNALS",
    sequence=SimpleNamespace(stream=42, consumer=7),
    timestamp=datetime(2026, 11, 6, tzinfo=timezone.utc),
  )
  assert delivery_key_for(msg) == "seq:SIGNALS:42@2026-11-06T00:00:00+00:00"


async def test_delivery_key_is_none_for_a_message_without_identity():
  from broker.services.signal_processing_service import delivery_key_for

  # FakeMsg has no headers and no metadata — like a hand-built test message.
  assert delivery_key_for(FakeMsg(_envelope())) is None


def _jetstream_msg(num_delivered: int = 1) -> FakeMsg:
  from datetime import datetime, timezone
  from types import SimpleNamespace

  msg = FakeMsg(_envelope())
  msg.metadata = SimpleNamespace(
    stream="SIGNALS",
    sequence=SimpleNamespace(stream=42, consumer=7),
    timestamp=datetime(2026, 11, 6, tzinfo=timezone.utc),
    num_delivered=num_delivered,
  )
  return msg


async def test_worker_passes_the_delivery_key_to_the_service():
  service = FakeService()
  worker = SignalWorker(service=service, connection=object())  # type: ignore[arg-type]
  await worker._handle_one(_jetstream_msg())
  assert service.delivery_keys == ["seq:SIGNALS:42@2026-11-06T00:00:00+00:00"]
  # First delivery: no redelivery lookup.
  assert service.attempts == [False]


async def test_a_redelivered_message_is_flagged():
  service = FakeService()
  worker = SignalWorker(service=service, connection=object())  # type: ignore[arg-type]
  await worker._handle_one(_jetstream_msg(num_delivered=2))
  assert service.attempts == [True]


async def test_a_transient_failure_is_retried_in_place_before_any_nak(monkeypatch):
  sleeps: list[float] = []

  async def fake_sleep(seconds):
    sleeps.append(seconds)

  monkeypatch.setattr(asyncio, "sleep", fake_sleep)
  service = FakeService(raise_exc=RuntimeError("db blip"), fail_times=2)
  worker = SignalWorker(
    service=service,
    connection=object(),  # type: ignore[arg-type]
    inline_retry_delays=(0.5, 1.0, 2.0),
  )
  msg = _jetstream_msg()
  await worker._handle_one(msg)

  # Held in place — the next envelope cannot overtake it — then acked.
  assert msg.acked is True
  assert msg.naked is False
  assert sleeps == [0.5, 1.0]
  # A retry may find the row its failed attempt wrote.
  assert service.attempts == [False, True, True]


async def test_invalid_utf8_envelope_is_termed():
  service = FakeService()
  worker = SignalWorker(service=service, connection=object())  # type: ignore[arg-type]
  msg = FakeMsg(b"\xff\xfe")
  await worker._handle_one(msg)
  assert msg.termed is True


async def test_an_unhandled_handler_error_does_not_end_the_loop(caplog):
  class ExplodingWorker(SignalWorker):
    async def _handle_one(self, msg):
      raise TypeError("unexpected")

  worker = ExplodingWorker(service=FakeService(), connection=object())  # type: ignore[arg-type]
  first, second = FakeMsg(_envelope()), FakeMsg(_envelope())
  worker._sub = FakeSub(worker, [[first, second]])
  await worker._run()
  # Both messages were attempted and handed back; the loop survived.
  assert first.naked is True and second.naked is True


async def test_handler_failure_is_naked_with_a_delay():
  from broker.services.jetstream_worker import NAK_DELAY_SECONDS

  service = FakeService(raise_exc=RuntimeError("db down"))
  worker = SignalWorker(service=service, connection=object(), inline_retry_delays=())  # type: ignore[arg-type]
  msg = FakeMsg(_envelope())
  await worker._handle_one(msg)
  assert msg.naked is True
  assert msg.nak_delay == NAK_DELAY_SECONDS


@pytest.mark.parametrize(
  "body",
  [
    b'{"payload": {"token": "super-secret", "strategy": ',
    b'{"token": "super-secret"}',
    b'{"payload": {"token": "super-secret", "strategy": "s"}}',
  ],
  ids=["malformed-json", "missing-payload", "invalid-payload"],
)
async def test_a_rejected_envelope_never_logs_the_webhook_token(body, caplog):
  worker = SignalWorker(service=FakeService(), connection=object())  # type: ignore[arg-type]
  msg = FakeMsg(body)
  with caplog.at_level(logging.ERROR):
    await worker._handle_one(msg)
  assert msg.termed is True
  assert "super-secret" not in caplog.text
