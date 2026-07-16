import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional

import pytest

from broker.constants import NOTIFICATION_TIMEZONE_KEY
from broker.schemas.core import SignalActionEnum, SignalStatusEnum
from broker.schemas.publisher_schema import TradingSignal
from broker.schemas.webhook_schema import PositionSchema, WebhookPayload
from broker.services.signal_processing_service import (
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
    # Rows keyed by str(uuid). Each row is a SimpleNamespace with attributes
    # matching the ORM Signal model fields the service touches.
    self._rows: dict[str, SimpleNamespace] = {}
    if existing is not None:
      self._rows[existing["id"]] = SimpleNamespace(**existing)

  async def log_signal(self, payload):
    self.logged.append(payload)
    if self.return_id == "__persist_fail__":
      return None
    row_id = self.return_id
    self._rows[row_id] = SimpleNamespace(
      id=uuid.UUID(row_id),
      status=SignalStatusEnum.QUEUED,
      attempts=settings.SIGNAL_MAX_ATTEMPTS,
      last_attempt=None,
      raw=payload.model_dump(mode="json"),
    )
    return row_id

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

  async def get(self, key):
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

  async def publish_webhook_event(self, *, signal_id, strategy, envelope):
    self.enqueued.append(
      {"signal_id": signal_id, "strategy": strategy, "envelope": envelope}
    )

  async def publish(self, signal):
    if self._publish_fails:
      raise RuntimeError("worker publish failed")
    self.published.append(signal)

  async def publish_flat(self, symbol, timestamp, strategy):
    if self._publish_fails:
      raise RuntimeError("worker publish failed")
    self.flats.append((symbol, timestamp, strategy))

  async def publish_admin_signal(self, **kwargs):
    return None

  async def publish_system_signal(self, **kwargs):
    return None

  async def publish_system_retry_signal(self, **kwargs):
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
):
  publisher = FakePublisher(publish_fails=publish_fails)
  notifier = FakeNotifier()
  signal_repo = FakeSignalRepository(return_id=signal_id)
  service = SignalProcessingService(
    signal_repository=signal_repo,
    setting_repository=FakeSettingRepository(blocked=blocked),
    publisher=publisher,
    notifier=notifier,
    webhook_secret=secret,
  )
  return service, publisher, notifier, signal_repo


# ── Enqueue path (webhook route) ─────────────────────────────────────


async def test_enqueue_only_pushes_to_jetstream_no_side_effects():
  service, publisher, notifier, signal_repo = _make_service()
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
  service, publisher, _, _ = _make_service()
  result = await service.enqueue(_payload(action=SignalActionEnum.FLAT))
  assert result["status"] == "queued"
  assert publisher.flats == []
  assert len(publisher.enqueued) == 1


async def test_invalid_token_raises_401():
  service, _, _, _ = _make_service()
  with pytest.raises(SignalError) as exc:
    await service.enqueue(_payload(token="wrong"))
  assert exc.value.status_code == 401


async def test_missing_secret_raises_500():
  service, _, _, _ = _make_service(secret="")
  with pytest.raises(SignalError) as exc:
    await service.enqueue(_payload())
  assert exc.value.status_code == 500


async def test_enqueue_does_not_check_block_gate():
  # Block gate lives in the handler now — the webhook must return fast.
  service, publisher, notifier, _ = _make_service(blocked=True)
  result = await service.enqueue(_payload())
  assert result["status"] == "queued"
  assert notifier.messages == []
  assert len(publisher.enqueued) == 1


async def test_enqueue_failure_raises_500():
  service, publisher, _, _ = _make_service()

  async def boom(**_kwargs):
    raise RuntimeError("jetstream down")

  publisher.publish_webhook_event = boom  # type: ignore[assignment]
  with pytest.raises(SignalError) as exc:
    await service.enqueue(_payload())
  assert exc.value.status_code == 500


# ── Handler path (JetStream consumer) ────────────────────────────────


async def test_handle_enqueued_persists_publishes_notifies_and_marks_published():
  service, publisher, notifier, signal_repo = _make_service()
  result = await service.handle_enqueued(payload=_payload())

  assert result["status"] == "accepted"
  assert len(signal_repo.logged) == 1
  assert len(publisher.published) == 1
  assert publisher.published[0].symbol == "XAUUSD"
  assert len(notifier.messages) == 1
  assert signal_repo.published_ids == [result["signal_id"]]


async def test_handle_enqueued_flat_uses_publish_flat():
  service, publisher, notifier, signal_repo = _make_service()
  result = await service.handle_enqueued(
    payload=_payload(action=SignalActionEnum.FLAT)
  )

  assert result["status"] == "accepted"
  assert publisher.flats == [
    ("XAUUSD", datetime(2026, 1, 1, tzinfo=timezone.utc), "strat")
  ]
  assert publisher.published == []
  assert signal_repo.published_ids == [result["signal_id"]]


async def test_handle_enqueued_blocked_signal_notifies_and_drops():
  service, publisher, notifier, signal_repo = _make_service(blocked=True)
  result = await service.handle_enqueued(payload=_payload())
  assert result["status"] == "blocked"
  # A blocked signal is not persisted and is not fanned out.
  assert signal_repo.logged == []
  assert publisher.published == []
  assert publisher.flats == []
  # But the operator is still notified.
  assert len(notifier.messages) == 1


async def test_handle_enqueued_persist_failure_raises_for_jetstream_redelivery():
  service, _, _, _ = _make_service(signal_id="__persist_fail__")
  with pytest.raises(RuntimeError):
    await service.handle_enqueued(payload=_payload())


async def test_handle_enqueued_publish_failure_records_attempt_and_returns():
  service, publisher, notifier, signal_repo = _make_service(publish_fails=True)
  result = await service.handle_enqueued(payload=_payload())
  assert result["status"] == "retry_scheduled"
  assert len(signal_repo.failed_ids) == 1
  # No notification / mark_published on a failed fan-out.
  assert notifier.messages == []
  assert signal_repo.published_ids == []
  # Row is still QUEUED with one attempt consumed.
  row = signal_repo._rows[result["signal_id"]]
  assert row.status == SignalStatusEnum.QUEUED
  assert row.attempts == settings.SIGNAL_MAX_ATTEMPTS - 1


# ── Retry path (retry job) ───────────────────────────────────────────


async def test_retry_signal_replays_fanout_and_marks_published_on_success():
  publisher = FakePublisher()
  notifier = FakeNotifier()
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
    webhook_secret="secret",
  )

  result = await service.retry_signal(signal_id)
  assert result["status"] == "accepted"
  assert len(publisher.published) == 1
  assert signal_repo.published_ids == [signal_id]
  # Second-attempt notification carries the Attempt marker.
  assert "Attempt:" in notifier.messages[0]


async def test_retry_signal_missing_row_returns_not_found():
  service, _, _, _ = _make_service()
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


# ── Notification timezone wiring (handler path) ──────────────────────


async def test_signal_notification_uses_configured_timezone():
  publisher = FakePublisher()
  notifier = FakeNotifier()
  setting_repo = FakeSettingRepository()
  setting_repo.values[NOTIFICATION_TIMEZONE_KEY] = "-5"
  service = SignalProcessingService(
    signal_repository=FakeSignalRepository(),
    setting_repository=setting_repo,
    publisher=publisher,
    notifier=notifier,
    webhook_secret="secret",
  )

  await service.handle_enqueued(payload=_payload())

  assert "Time: 2025-12-31 19:00:00 (UTC-5)" in notifier.messages[0]


async def test_flat_notification_uses_configured_timezone():
  publisher = FakePublisher()
  notifier = FakeNotifier()
  setting_repo = FakeSettingRepository()
  setting_repo.values[NOTIFICATION_TIMEZONE_KEY] = "0"
  service = SignalProcessingService(
    signal_repository=FakeSignalRepository(),
    setting_repository=setting_repo,
    publisher=publisher,
    notifier=notifier,
    webhook_secret="secret",
  )

  await service.handle_enqueued(payload=_payload(action=SignalActionEnum.FLAT))

  assert "Time: 2026-01-01 00:00:00 (UTC+0)" in notifier.messages[0]


async def test_signal_notification_defaults_to_utc_plus_7_when_unset():
  service, _, notifier, _ = _make_service()
  await service.handle_enqueued(payload=_payload())
  assert "Time: 2026-01-01 07:00:00 (UTC+7)" in notifier.messages[0]


# ── Attempt line on notifications ────────────────────────────────────


async def test_first_attempt_notification_has_no_attempt_line():
  service, _, notifier, _ = _make_service()
  await service.handle_enqueued(payload=_payload())
  assert "Attempt:" not in notifier.messages[0]


async def test_second_and_third_attempts_show_attempt_line():
  publisher = FakePublisher()
  notifier = FakeNotifier()
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
    notifier=notifier,
    webhook_secret="secret",
  )

  await service.retry_signal("11111111-1111-1111-1111-111111111111")
  await service.retry_signal("22222222-2222-2222-2222-222222222222")

  # attempts=2 → 2nd attempt overall, attempts=1 → 3rd attempt overall.
  assert "Attempt: <b>2</b>" in notifier.messages[0]
  assert "Attempt: <b>3</b>" in notifier.messages[1]
