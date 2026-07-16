from datetime import datetime, timezone

import pytest

from broker.constants import NOTIFICATION_TIMEZONE_KEY
from broker.schemas.core import SignalActionEnum
from broker.schemas.publisher_schema import TradingSignal
from broker.schemas.webhook_schema import PositionSchema, WebhookPayload
from broker.services.signal_processing_service import (
  SignalError,
  SignalProcessingService,
)


# ── In-memory fakes implementing the Protocols ──────────────────────


class FakeSignalRepository:
  def __init__(self, return_id: str | None = "sig-id"):
    self.return_id = return_id
    self.logged: list[WebhookPayload] = []
    self.published_ids: list[str] = []

  async def log_signal(self, payload):
    self.logged.append(payload)
    return self.return_id

  async def mark_published(self, signal_id: str) -> bool:
    self.published_ids.append(signal_id)
    return True

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
  def __init__(self):
    self.enqueued: list[dict] = []
    self.published: list[TradingSignal] = []
    self.flats: list[tuple] = []

  async def publish_webhook_event(self, *, signal_id, strategy, envelope):
    self.enqueued.append(
      {"signal_id": signal_id, "strategy": strategy, "envelope": envelope}
    )

  async def publish(self, signal):
    self.published.append(signal)

  async def publish_flat(self, symbol, timestamp, strategy):
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
  *, blocked=False, signal_id="sig-id", secret="secret"
) -> tuple[SignalProcessingService, FakePublisher, FakeNotifier, FakeSignalRepository]:
  publisher = FakePublisher()
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


async def test_enqueue_persists_and_pushes_to_jetstream():
  service, publisher, notifier, signal_repo = _make_service()
  result = await service.process(_payload())

  assert result["status"] == "queued"
  assert result["signal_id"] == "sig-id"
  # Fast path only: no worker publish, no notify.
  assert publisher.published == []
  assert notifier.messages == []
  # But the envelope is durably queued on JetStream.
  assert len(publisher.enqueued) == 1
  enq = publisher.enqueued[0]
  assert enq["strategy"] == "strat"
  assert enq["signal_id"] == "sig-id"
  assert enq["envelope"]["signal_id"] == "sig-id"
  assert enq["envelope"]["payload"]["symbol"] == "OANDA:XAUUSD"


async def test_enqueue_of_flat_signal_also_uses_jetstream():
  service, publisher, notifier, _ = _make_service()
  result = await service.process(_payload(action=SignalActionEnum.FLAT))

  assert result["status"] == "queued"
  # FLAT is enqueued exactly like a regular signal — the worker splits later.
  assert publisher.flats == []
  assert len(publisher.enqueued) == 1
  assert publisher.enqueued[0]["strategy"] == "strat"


async def test_invalid_token_raises_401():
  service, _, _, _ = _make_service()
  with pytest.raises(SignalError) as exc:
    await service.process(_payload(token="wrong"))
  assert exc.value.status_code == 401


async def test_missing_secret_raises_500():
  service, _, _, _ = _make_service(secret="")
  with pytest.raises(SignalError) as exc:
    await service.process(_payload())
  assert exc.value.status_code == 500


async def test_blocked_raises_403_and_notifies():
  service, publisher, notifier, _ = _make_service(blocked=True)
  with pytest.raises(SignalError) as exc:
    await service.process(_payload())
  assert exc.value.status_code == 403
  assert len(notifier.messages) == 1  # blocked notification
  assert publisher.enqueued == []


async def test_persist_failure_raises_500():
  service, _, _, _ = _make_service(signal_id=None)
  with pytest.raises(SignalError) as exc:
    await service.process(_payload())
  assert exc.value.status_code == 500


async def test_enqueue_failure_raises_500():
  service, publisher, _, _ = _make_service()

  async def boom(**_kwargs):
    raise RuntimeError("jetstream down")

  publisher.publish_webhook_event = boom  # type: ignore[assignment]
  with pytest.raises(SignalError) as exc:
    await service.process(_payload())
  assert exc.value.status_code == 500


# ── Handler path (JetStream consumer) ────────────────────────────────


async def test_handle_enqueued_publishes_notifies_and_marks_published():
  service, publisher, notifier, signal_repo = _make_service()
  result = await service.handle_enqueued(signal_id="sig-id", payload=_payload())

  assert result["status"] == "accepted"
  assert len(publisher.published) == 1
  assert publisher.published[0].symbol == "XAUUSD"
  assert len(notifier.messages) == 1
  assert signal_repo.published_ids == ["sig-id"]


async def test_handle_enqueued_flat_uses_publish_flat():
  service, publisher, notifier, signal_repo = _make_service()
  result = await service.handle_enqueued(
    signal_id="sig-id", payload=_payload(action=SignalActionEnum.FLAT)
  )

  assert result["status"] == "accepted"
  assert publisher.flats == [
    ("XAUUSD", datetime(2026, 1, 1, tzinfo=timezone.utc), "strat")
  ]
  assert publisher.published == []
  assert signal_repo.published_ids == ["sig-id"]


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

  await service.handle_enqueued(signal_id="sig-id", payload=_payload())

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

  await service.handle_enqueued(
    signal_id="sig-id", payload=_payload(action=SignalActionEnum.FLAT)
  )

  assert "Time: 2026-01-01 00:00:00 (UTC+0)" in notifier.messages[0]


async def test_signal_notification_defaults_to_utc_plus_7_when_unset():
  service, _, notifier, _ = _make_service()
  await service.handle_enqueued(signal_id="sig-id", payload=_payload())
  assert "Time: 2026-01-01 07:00:00 (UTC+7)" in notifier.messages[0]
