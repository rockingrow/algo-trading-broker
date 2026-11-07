import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional

import pytest

from broker.schemas.core import SignalActionEnum, SignalStatusEnum
from broker.schemas.publisher_schema import TradingSignal
from broker.schemas.webhook_schema import PositionSchema, WebhookPayload
from broker.services.signal_processing_service import (
  DeferredEnqueuer,
  SignalError,
  SignalProcessingService,
)
from broker.settings import settings


# ── In-memory fakes implementing the Protocols ──────────────────────


class FakeSignalRepository:
  def __init__(self, return_id: str | None = None, existing: dict | None = None):
    self.return_id = return_id or str(uuid.uuid4())
    self.logged: list[WebhookPayload] = []
    self.published_ids: list[str] = []
    self.failed_ids: list[str] = []
    self.marked_failed_ids: list[str] = []
    # Rows keyed by str(uuid). Each row is a SimpleNamespace with attributes
    # matching the ORM Signal model fields the service touches.
    self._rows: dict[str, SimpleNamespace] = {}
    if existing is not None:
      self._rows[existing["id"]] = SimpleNamespace(**existing)

  async def log_signal(self, payload, *, delivery_key=None, attempts=None):
    self.logged.append(payload)
    if self.return_id == "__persist_fail__":
      return None
    row_id = self.return_id
    self._rows[row_id] = SimpleNamespace(
      id=uuid.UUID(row_id),
      status=SignalStatusEnum.QUEUED,
      attempts=attempts if attempts is not None else settings.signal.MAX_ATTEMPTS,
      last_attempt=None,
      delivery_key=delivery_key,
      raw=payload.model_dump(mode="json"),
    )
    return row_id

  async def get_by_delivery_key(self, delivery_key):
    for row in self._rows.values():
      if getattr(row, "delivery_key", None) == delivery_key:
        return row
    return None

  async def mark_failed(self, signal_id):
    self.marked_failed_ids.append(signal_id)
    row = self._rows.get(signal_id)
    if row is None:
      return False
    row.status = SignalStatusEnum.FAILED
    row.attempts = 0
    return True

  async def mark_published(self, signal_id: str) -> bool:
    self.published_ids.append(signal_id)
    row = self._rows.get(signal_id)
    if row is not None:
      row.status = SignalStatusEnum.PUBLISHED
    return True

  async def get_by_id(self, signal_id: str) -> Optional[SimpleNamespace]:
    return self._rows.get(signal_id)

  async def record_attempt_failure(self, signal_id: str) -> Optional[SimpleNamespace]:
    self.failed_ids.append(signal_id)
    row = self._rows.get(signal_id)
    if row is None:
      return None
    row.last_attempt = datetime.now(timezone.utc)
    if row.attempts <= 1:
      row.attempts = 0
      row.status = SignalStatusEnum.FAILED
    else:
      row.attempts -= 1
    return row

  async def list_retryable(self, retry_interval_seconds: int):
    return []

  async def list_recent_by_strategies(self, strategies, since_seconds):
    return []


class FakeSettingRepository:
  def __init__(self, blocked: bool = False):
    self.values = {"signal_blocked": "1" if blocked else "0"}
    self.read_fails = False

  async def get(self, key):
    return self.values.get(key)

  async def get_strict(self, key):
    if self.read_fails:
      raise RuntimeError("database unavailable")
    return self.values.get(key)

  async def get_many(self, keys):
    return {k: v for k, v in self.values.items() if k in keys and v is not None}

  async def set(self, key, value):
    self.values[key] = value
    return True


class FakePublisher:
  def __init__(self, publish_fails: bool = False):
    self.enqueued: list[dict] = []
    self.published: list[TradingSignal] = []
    self.flats: list[tuple] = []
    self._publish_fails = publish_fails
    # Set to make the next N enqueues fail, standing in for a NATS blip.
    self.enqueue_failures = 0
    self.enqueue_hangs = False

  async def publish_webhook_event(
    self, *, signal_id, strategy, envelope, timeout=None, msg_id=None
  ):
    if self.enqueue_hangs:
      await asyncio.sleep(3600)
    if self.enqueue_failures > 0:
      self.enqueue_failures -= 1
      raise RuntimeError("jetstream unavailable")
    self.enqueued.append(
      {
        "signal_id": signal_id,
        "strategy": strategy,
        "envelope": envelope,
        "timeout": timeout,
        "msg_id": msg_id,
      }
    )

  async def publish(self, signal):
    if self._publish_fails:
      raise RuntimeError("worker publish failed")
    self.published.append(signal)

  async def publish_flat(
    self, *, signal_id, signal_uxid=None, symbol, timestamp, strategy
  ):
    if self._publish_fails:
      raise RuntimeError("worker publish failed")
    self.flats.append((signal_id, signal_uxid, symbol, timestamp, strategy))

  async def publish_admin_signal(self, **kwargs):
    return None

  async def publish_system_signal(self, **kwargs):
    return None

  async def publish_system_ack(self, **kwargs):
    return None

  async def publish_system_error(self, **kwargs):
    return None


class FakeNotifier:
  def __init__(self):
    self.messages: list[str] = []

  async def send_message(self, message_text):
    self.messages.append(message_text)


class FakeBroadcaster:
  """Records what the service handed to the broadcaster.

  The service no longer formats the signal itself — that is the dispatcher's
  job — so a test that used to inspect the rendered message body now checks
  what payload / attempt number the broadcaster was fed instead.
  """

  def __init__(self):
    self.calls: list[tuple[WebhookPayload, int | None]] = []

  async def broadcast(self, payload, *, attempt_number=None):
    self.calls.append((payload, attempt_number))


async def _wait_until(predicate, timeout: float = 2.0) -> None:
  """Poll *predicate* until it holds, so tests never sleep on a fixed guess."""
  loop = asyncio.get_running_loop()
  deadline = loop.time() + timeout
  while loop.time() < deadline:
    if predicate():
      return
    await asyncio.sleep(0.01)
  raise AssertionError("condition not reached within timeout")


def _payload(action=SignalActionEnum.LONG, token="secret", **overrides):
  base = dict(
    strategy="strat",
    symbol="OANDA:XAUUSD",
    timeframe="60",
    timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
    position=PositionSchema(action=action, price=100.0, quantity=1.0),
    token=token,
  )
  base.update(overrides)
  return WebhookPayload(**base)


def _make_service(
  *,
  blocked=False,
  signal_id=None,
  secret="secret",
  publish_fails=False,
  deferred_enqueuer=None,
  broadcaster=None,
):
  publisher = FakePublisher(publish_fails=publish_fails)
  notifier = FakeNotifier()
  broadcaster = broadcaster if broadcaster is not None else FakeBroadcaster()
  signal_repo = FakeSignalRepository(return_id=signal_id)
  service = SignalProcessingService(
    signal_repository=signal_repo,
    setting_repository=FakeSettingRepository(blocked=blocked),
    publisher=publisher,
    notifier=notifier,
    broadcaster=broadcaster,
    webhook_secret=secret,
    deferred_enqueuer=deferred_enqueuer,
  )
  return service, publisher, notifier, broadcaster, signal_repo


# ── Enqueue path (webhook route) ─────────────────────────────────────


async def test_enqueue_only_pushes_to_jetstream_no_side_effects():
  service, publisher, notifier, broadcaster, signal_repo = _make_service()
  result = await service.enqueue(_payload())

  assert result["status"] == "queued"
  # The webhook route is a fast path: no DB write, no notify, no worker publish.
  assert signal_repo.logged == []
  assert publisher.published == []
  assert publisher.flats == []
  assert notifier.messages == []
  # Only the JetStream enqueue happens.
  assert len(publisher.enqueued) == 1
  enq = publisher.enqueued[0]
  assert enq["strategy"] == "strat"
  assert enq["envelope"]["payload"]["symbol"] == "OANDA:XAUUSD"
  # No signal_id yet — DB row is created in the handler.
  assert enq["signal_id"] == ""


async def test_enqueue_of_flat_signal_also_uses_jetstream():
  service, publisher, _, _, _ = _make_service()
  result = await service.enqueue(_payload(action=SignalActionEnum.FLAT))
  assert result["status"] == "queued"
  assert publisher.flats == []
  assert len(publisher.enqueued) == 1


async def test_invalid_token_raises_401():
  service, _, _, _, _ = _make_service()
  with pytest.raises(SignalError) as exc:
    await service.enqueue(_payload(token="wrong"))
  assert exc.value.status_code == 401


async def test_missing_secret_raises_500():
  service, _, _, _, _ = _make_service(secret="")
  with pytest.raises(SignalError) as exc:
    await service.enqueue(_payload())
  assert exc.value.status_code == 500


async def test_enqueue_does_not_check_block_gate():
  # Block gate lives in the handler now — the webhook must return fast.
  service, publisher, notifier, _, _ = _make_service(blocked=True)
  result = await service.enqueue(_payload())
  assert result["status"] == "queued"
  assert notifier.messages == []
  assert len(publisher.enqueued) == 1


async def test_enqueue_failure_without_a_deferred_queue_raises_503():
  service, publisher, _, _, _ = _make_service()

  async def boom(**_kwargs):
    raise RuntimeError("jetstream down")

  publisher.publish_webhook_event = boom  # type: ignore[assignment]
  with pytest.raises(SignalError) as exc:
    await service.enqueue(_payload())
  # 503, not a hung request: TradingView reports a refusal it can show the
  # operator instead of "request took too long and timed out".
  assert exc.value.status_code == 503


async def test_enqueue_carries_the_deadline_and_a_dedup_id():
  service, publisher, _, _, _ = _make_service()
  await service.enqueue(_payload())

  enq = publisher.enqueued[0]
  assert enq["timeout"] == settings.webhook.ENQUEUE_TIMEOUT
  assert enq["msg_id"]


async def test_slow_enqueue_is_deferred_and_still_answers(monkeypatch):
  monkeypatch.setattr(settings.webhook, "ENQUEUE_TIMEOUT", 0.05)
  deferred = DeferredEnqueuer(FakePublisher(), interval_seconds=0.01)
  service, publisher, _, _, _ = _make_service(deferred_enqueuer=deferred)
  publisher.enqueue_hangs = True

  started = asyncio.get_running_loop().time()
  result = await service.enqueue(_payload())
  elapsed = asyncio.get_running_loop().time() - started

  # The alert is answered inside the deadline instead of waiting out nats-py's
  # 5s PubAck timeout, and the envelope is kept for the background retry.
  assert result["status"] == "deferred"
  assert elapsed < 1.0
  assert deferred.pending == 1


async def test_deferred_enqueue_retries_until_jetstream_accepts():
  publisher = FakePublisher()
  publisher.enqueue_failures = 2
  deferred = DeferredEnqueuer(publisher, interval_seconds=0.01)
  await deferred.start()
  try:
    deferred.submit(strategy="strat", envelope={"payload": {}}, msg_id="mid-1")
    await _wait_until(lambda: publisher.enqueued)
  finally:
    await deferred.stop()

  assert len(publisher.enqueued) == 1
  # Same id on every attempt, so an enqueue whose first ack was merely slow is
  # dropped by JetStream rather than replayed into a second position.
  assert publisher.enqueued[0]["msg_id"] == "mid-1"


async def test_deferred_enqueue_gives_up_after_max_attempts():
  publisher = FakePublisher()
  publisher.enqueue_failures = 99
  deferred = DeferredEnqueuer(publisher, interval_seconds=0.01, max_attempts=3)
  await deferred.start()
  try:
    deferred.submit(strategy="strat", envelope={"payload": {}}, msg_id="mid-2")
    await _wait_until(lambda: deferred.pending == 0 and publisher.enqueue_failures < 99)
    await asyncio.sleep(0.05)
  finally:
    await deferred.stop()

  # 99 - 3 attempts spent; the envelope is dropped rather than retried forever.
  assert publisher.enqueue_failures == 96
  assert publisher.enqueued == []


async def test_enqueue_reports_503_when_the_backlog_is_full():
  deferred = DeferredEnqueuer(FakePublisher(), interval_seconds=0.01, maxsize=1)
  service, publisher, _, _, _ = _make_service(deferred_enqueuer=deferred)
  publisher.enqueue_failures = 2

  first = await service.enqueue(_payload())
  assert first["status"] == "deferred"

  with pytest.raises(SignalError) as exc:
    await service.enqueue(_payload())
  assert exc.value.status_code == 503


# ── Handler path (JetStream consumer) ────────────────────────────────


async def test_handle_enqueued_persists_publishes_notifies_and_marks_published():
  service, publisher, notifier, broadcaster, signal_repo = _make_service()
  result = await service.handle_enqueued(payload=_payload())

  assert result["status"] == "accepted"
  assert len(signal_repo.logged) == 1
  assert len(publisher.published) == 1
  assert publisher.published[0].symbol == "XAUUSD"
  # The signal itself is handed to the broadcaster, not to the notifier —
  # a signal is one line in a longer-lived cycle message.
  assert len(broadcaster.calls) == 1
  assert notifier.messages == []
  assert signal_repo.published_ids == [result["signal_id"]]


async def test_handle_enqueued_flat_uses_publish_flat():
  service, publisher, notifier, broadcaster, signal_repo = _make_service()
  payload = _payload(action=SignalActionEnum.FLAT)
  result = await service.handle_enqueued(payload=payload)

  assert result["status"] == "accepted"
  # signal_id identifies this directive; signal_uxid ties it back to the cycle.
  assert publisher.flats == [
    (
      result["signal_id"],
      payload.signal_uxid,
      "XAUUSD",
      datetime(2026, 1, 1, tzinfo=timezone.utc),
      "strat",
    )
  ]
  assert publisher.published == []
  assert signal_repo.published_ids == [result["signal_id"]]


async def test_handle_enqueued_blocked_signal_notifies_and_drops():
  service, publisher, notifier, broadcaster, signal_repo = _make_service(blocked=True)
  result = await service.handle_enqueued(payload=_payload())
  assert result["status"] == "blocked"
  # A blocked signal is not persisted and is not fanned out.
  assert signal_repo.logged == []
  assert publisher.published == []
  assert publisher.flats == []
  # But the operator is still notified — a *block* is an operational event, not
  # a signal, so it stays on the notifier and never reaches the broadcaster.
  assert len(notifier.messages) == 1
  assert broadcaster.calls == []


async def test_handle_enqueued_persist_failure_raises_for_jetstream_redelivery():
  service, _, _, _, _ = _make_service(signal_id="__persist_fail__")
  with pytest.raises(RuntimeError):
    await service.handle_enqueued(payload=_payload())


async def test_handle_enqueued_publish_failure_records_attempt_and_returns():
  service, publisher, notifier, broadcaster, signal_repo = _make_service(
    publish_fails=True
  )
  result = await service.handle_enqueued(payload=_payload())
  assert result["status"] == "retry_scheduled"
  assert len(signal_repo.failed_ids) == 1
  # No broadcast / mark_published on a failed fan-out — the signal never
  # reached workers, so it must not appear in the cycle message either.
  assert notifier.messages == []
  assert broadcaster.calls == []
  assert signal_repo.published_ids == []
  # Row is still QUEUED with one attempt consumed.
  row = signal_repo._rows[result["signal_id"]]
  assert row.status == SignalStatusEnum.QUEUED
  assert row.attempts == settings.signal.MAX_ATTEMPTS - 1


# ── Retry path (retry job) ───────────────────────────────────────────


async def test_retry_signal_replays_fanout_and_marks_published_on_success():
  publisher = FakePublisher()
  notifier = FakeNotifier()
  broadcaster = FakeBroadcaster()
  signal_id = str(uuid.uuid4())
  signal_repo = FakeSignalRepository(
    existing={
      "id": signal_id,
      "status": SignalStatusEnum.QUEUED,
      "attempts": 2,
      "last_attempt": datetime(2026, 1, 1, tzinfo=timezone.utc),
      "raw": _payload().model_dump(mode="json"),
    }
  )
  service = SignalProcessingService(
    signal_repository=signal_repo,
    setting_repository=FakeSettingRepository(),
    publisher=publisher,
    notifier=notifier,
    broadcaster=broadcaster,
    webhook_secret="secret",
  )

  result = await service.retry_signal(signal_id)
  assert result["status"] == "accepted"
  assert len(publisher.published) == 1
  assert signal_repo.published_ids == [signal_id]
  # Second attempt overall: attempts=2 on entry → attempt number 2.
  assert broadcaster.calls[0][1] == 2


async def test_retry_signal_missing_row_returns_not_found():
  service, _, _, _, _ = _make_service()
  result = await service.retry_signal(str(uuid.uuid4()))
  assert result["status"] == "not_found"


async def test_retry_signal_skips_when_already_published():
  publisher = FakePublisher()
  signal_id = str(uuid.uuid4())
  signal_repo = FakeSignalRepository(
    existing={
      "id": signal_id,
      "status": SignalStatusEnum.PUBLISHED,
      "attempts": 0,
      "last_attempt": None,
      "raw": _payload().model_dump(mode="json"),
    }
  )
  service = SignalProcessingService(
    signal_repository=signal_repo,
    setting_repository=FakeSettingRepository(),
    publisher=publisher,
    notifier=FakeNotifier(),
    webhook_secret="secret",
  )
  result = await service.retry_signal(signal_id)
  assert result["status"] == "skipped"
  assert publisher.published == []


async def test_retry_signal_publish_failure_records_attempt_failure():
  publisher = FakePublisher(publish_fails=True)
  signal_id = str(uuid.uuid4())
  signal_repo = FakeSignalRepository(
    existing={
      "id": signal_id,
      "status": SignalStatusEnum.QUEUED,
      "attempts": 2,
      "last_attempt": None,
      "raw": _payload().model_dump(mode="json"),
    }
  )
  service = SignalProcessingService(
    signal_repository=signal_repo,
    setting_repository=FakeSettingRepository(),
    publisher=publisher,
    notifier=FakeNotifier(),
    webhook_secret="secret",
  )
  result = await service.retry_signal(signal_id)
  assert result["status"] == "retry_scheduled"
  assert signal_repo.failed_ids == [signal_id]


# ── Broadcaster wiring (handler + retry path) ────────────────────────


async def test_handle_enqueued_hands_payload_to_broadcaster():
  """A fanned-out signal is fed to the broadcaster with the entry payload."""
  service, _, _, broadcaster, _ = _make_service()
  payload = _payload()
  await service.handle_enqueued(payload=payload)
  assert len(broadcaster.calls) == 1
  fed_payload, attempt = broadcaster.calls[0]
  assert fed_payload.signal_uxid == payload.signal_uxid
  # First attempt has no marker — it is the default path.
  assert attempt is None


async def test_broadcaster_receives_flat_the_same_way():
  service, _, _, broadcaster, _ = _make_service()
  await service.handle_enqueued(payload=_payload(action=SignalActionEnum.FLAT))
  assert len(broadcaster.calls) == 1
  assert broadcaster.calls[0][0].position.action == SignalActionEnum.FLAT


async def test_broadcast_failure_does_not_block_mark_published():
  """A Telegram outage must never roll back a successful worker publish."""

  class ExplodingBroadcaster:
    async def broadcast(self, payload, *, attempt_number=None):
      raise RuntimeError("telegram down")

  service, _, _, _, signal_repo = _make_service(broadcaster=ExplodingBroadcaster())
  result = await service.handle_enqueued(payload=_payload())
  assert result["status"] == "accepted"
  assert signal_repo.published_ids == [result["signal_id"]]


async def test_second_and_third_attempts_carry_the_attempt_number():
  publisher = FakePublisher()
  broadcaster = FakeBroadcaster()
  # Two rows so we can retry each once with attempts=2 and attempts=1.
  raw = _payload().model_dump(mode="json")
  signal_repo = FakeSignalRepository()
  signal_repo._rows["11111111-1111-1111-1111-111111111111"] = SimpleNamespace(
    id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
    status=SignalStatusEnum.QUEUED,
    attempts=2,
    last_attempt=None,
    raw=raw,
  )
  signal_repo._rows["22222222-2222-2222-2222-222222222222"] = SimpleNamespace(
    id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
    status=SignalStatusEnum.QUEUED,
    attempts=1,
    last_attempt=None,
    raw=raw,
  )
  service = SignalProcessingService(
    signal_repository=signal_repo,
    setting_repository=FakeSettingRepository(),
    publisher=publisher,
    notifier=FakeNotifier(),
    broadcaster=broadcaster,
    webhook_secret="secret",
  )

  await service.retry_signal("11111111-1111-1111-1111-111111111111")
  await service.retry_signal("22222222-2222-2222-2222-222222222222")

  # attempts=2 → 2nd attempt overall, attempts=1 → 3rd attempt overall.
  assert [n for _, n in broadcaster.calls] == [2, 3]


async def test_broadcaster_is_optional():
  """Wiring the service without a broadcaster silently disables broadcasts."""
  publisher = FakePublisher()
  signal_repo = FakeSignalRepository()
  service = SignalProcessingService(
    signal_repository=signal_repo,
    setting_repository=FakeSettingRepository(),
    publisher=publisher,
    notifier=FakeNotifier(),
    webhook_secret="secret",
  )
  result = await service.handle_enqueued(payload=_payload())
  # The signal still gets fanned out and marked published; the broadcast just
  # never happens, which is what "no broadcaster" is supposed to mean.
  assert result["status"] == "accepted"
  assert signal_repo.published_ids == [result["signal_id"]]


# ── Redelivery, fail-closed gate, permanent parse failures ───────────


async def test_redelivery_of_a_published_signal_is_not_fanned_out_again():
  service, publisher, notifier, broadcaster, signal_repo = _make_service()
  first = await service.handle_enqueued(payload=_payload(), delivery_key="seq:abc")
  assert first["status"] == "accepted"

  # JetStream hands the same message back (its ack was lost).
  again = await service.handle_enqueued(
    payload=_payload(), delivery_key="seq:abc", redelivered=True
  )

  assert again == {"status": "duplicate", "signal_id": first["signal_id"]}
  assert len(signal_repo.logged) == 1
  assert len(publisher.published) == 1


async def test_redelivery_of_an_interrupted_first_fanout_reuses_the_signal_id():
  service, publisher, notifier, broadcaster, signal_repo = _make_service()
  signal_id = signal_repo.return_id
  # The first delivery persisted the row, then the broker died before the
  # fan-out was confirmed: QUEUED, no attempt recorded.
  await signal_repo.log_signal(_payload(), delivery_key="seq:abc")

  result = await service.handle_enqueued(
    payload=_payload(), delivery_key="seq:abc", redelivered=True
  )

  assert result["status"] == "accepted"
  assert result["signal_id"] == signal_id
  # No second row, and the workers see the id they de-duplicate on.
  assert len(signal_repo.logged) == 1
  assert [signal.signal_id for signal in publisher.published] == [signal_id]


async def test_redelivery_with_a_recorded_failure_is_left_to_the_retry_job():
  service, publisher, notifier, broadcaster, signal_repo = _make_service()
  signal_id = await signal_repo.log_signal(_payload(), delivery_key="seq:abc")
  await signal_repo.record_attempt_failure(signal_id)

  result = await service.handle_enqueued(
    payload=_payload(), delivery_key="seq:abc", redelivered=True
  )

  assert result["status"] == "duplicate"
  assert publisher.published == []


async def test_signals_without_a_delivery_key_are_persisted_each_time():
  service, publisher, notifier, broadcaster, signal_repo = _make_service()
  await service.handle_enqueued(payload=_payload())
  await service.handle_enqueued(payload=_payload())
  assert len(signal_repo.logged) == 2


async def test_block_gate_fails_closed_when_the_setting_cannot_be_read():
  service, publisher, notifier, broadcaster, signal_repo = _make_service()
  service._settings.read_fails = True

  with pytest.raises(RuntimeError):
    await service.handle_enqueued(payload=_payload(), delivery_key="seq:abc")

  # Nothing persisted, nothing sent: the worker NAKs and JetStream redelivers.
  assert signal_repo.logged == []
  assert publisher.published == []


async def test_parse_failure_marks_the_signal_failed_without_retrying(monkeypatch):
  import broker.services.signal_processing_service as module

  def broken_parse(payload, signal_id):
    raise ValueError("unknown timeframe")

  monkeypatch.setattr(module, "parse_signal", broken_parse)
  service, publisher, notifier, broadcaster, signal_repo = _make_service()

  result = await service.handle_enqueued(payload=_payload())

  assert result["status"] == "invalid"
  assert signal_repo.marked_failed_ids == [result["signal_id"]]
  assert signal_repo.failed_ids == []
  assert signal_repo._rows[result["signal_id"]].status == SignalStatusEnum.FAILED
  assert broadcaster.calls == []


async def test_max_attempts_and_enqueue_timeout_can_be_injected():
  publisher = FakePublisher()
  signal_repo = FakeSignalRepository()
  service = SignalProcessingService(
    signal_repository=signal_repo,
    setting_repository=FakeSettingRepository(),
    publisher=publisher,
    notifier=FakeNotifier(),
    webhook_secret="secret",
    enqueue_timeout=0.25,
    max_attempts=7,
  )
  await service.enqueue(_payload())
  assert publisher.enqueued[0]["timeout"] == 0.25

  result = await service.handle_enqueued(payload=_payload(), delivery_key=None)
  assert result["status"] == "accepted"
  # The row's budget is the injected one, not the settings default.
  assert signal_repo._rows[result["signal_id"]].attempts == 7


# ── DeferredEnqueuer shutdown drain ─────────────────────────────────


async def test_stop_flushes_envelopes_still_queued():
  publisher = FakePublisher()
  # Never started: the envelope sits in the queue, as it would behind a
  # NATS outage, until shutdown.
  deferred = DeferredEnqueuer(publisher, interval_seconds=10)
  deferred.submit(strategy="strat", envelope={"payload": {}}, msg_id="mid-1")

  await deferred.stop()

  assert [item["msg_id"] for item in publisher.enqueued] == ["mid-1"]
  assert deferred.pending == 0


async def test_stop_drains_the_envelope_waiting_out_its_retry_interval(caplog):
  publisher = FakePublisher()
  publisher.enqueue_failures = 1
  deferred = DeferredEnqueuer(publisher, interval_seconds=10, max_attempts=5)
  await deferred.start()
  deferred.submit(strategy="strat", envelope={"payload": {}}, msg_id="mid-1")
  # The first attempt fails; the loop now sleeps with the envelope off the
  # queue. Cancelling it there used to lose the envelope silently.
  await _wait_until(lambda: publisher.enqueue_failures == 0)
  assert deferred.pending == 1

  await deferred.stop()

  assert [item["msg_id"] for item in publisher.enqueued] == ["mid-1"]


async def test_stop_reports_envelopes_it_could_not_flush(caplog):
  import logging

  publisher = FakePublisher()
  publisher.enqueue_failures = 10
  deferred = DeferredEnqueuer(publisher, interval_seconds=10)
  deferred.submit(strategy="strat", envelope={"payload": {}}, msg_id="mid-1")

  with caplog.at_level(logging.ERROR):
    await deferred.stop()

  assert publisher.enqueued == []
  assert "1 envelope(s) never queued" in caplog.text


async def test_a_first_delivery_does_not_look_the_key_up():
  service, publisher, notifier, broadcaster, signal_repo = _make_service()
  lookups = []
  original = signal_repo.get_by_delivery_key

  async def counting_lookup(key):
    lookups.append(key)
    return await original(key)

  signal_repo.get_by_delivery_key = counting_lookup
  await service.handle_enqueued(payload=_payload(), delivery_key="seq:abc")
  assert lookups == []


async def test_a_blocked_redelivery_closes_its_interrupted_row():
  service, publisher, notifier, broadcaster, signal_repo = _make_service(blocked=True)
  signal_id = await signal_repo.log_signal(_payload(), delivery_key="seq:abc")

  result = await service.handle_enqueued(
    payload=_payload(), delivery_key="seq:abc", redelivered=True
  )

  assert result == {"status": "blocked"}
  # Not left QUEUED with no attempt, where nothing would ever revisit it.
  assert signal_repo._rows[signal_id].status == SignalStatusEnum.FAILED
  assert publisher.published == []


async def test_an_unrecorded_publish_failure_is_handed_back_to_jetstream():
  service, publisher, notifier, broadcaster, signal_repo = _make_service(
    publish_fails=True
  )

  async def cannot_record(signal_id):
    return None

  signal_repo.record_attempt_failure = cannot_record
  with pytest.raises(RuntimeError, match="could not be recorded"):
    await service.handle_enqueued(payload=_payload(), delivery_key="seq:abc")


async def test_an_unrecorded_parse_failure_is_handed_back_to_jetstream(monkeypatch):
  import broker.services.signal_processing_service as module

  def broken_parse(payload, signal_id):
    raise ValueError("unknown timeframe")

  monkeypatch.setattr(module, "parse_signal", broken_parse)
  service, publisher, notifier, broadcaster, signal_repo = _make_service()

  async def cannot_mark(signal_id):
    return False

  signal_repo.mark_failed = cannot_mark
  with pytest.raises(RuntimeError, match="FAILED"):
    await service.handle_enqueued(payload=_payload())
