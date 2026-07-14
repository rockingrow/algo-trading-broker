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

  async def log_signal(self, payload):
    self.logged.append(payload)
    return self.return_id


class FakeSettingRepository:
  def __init__(self, blocked: bool = False):
    self.values = {"signal_blocked": "1" if blocked else "0"}

  async def get(self, key):
    return self.values.get(key)

  async def set(self, key, value):
    self.values[key] = value
    return True


class FakePublisher:
  def __init__(self):
    self.published: list[TradingSignal] = []
    self.flats: list[tuple] = []

  async def publish(self, signal):
    self.published.append(signal)

  async def publish_flat(self, symbol, timestamp, strategy):
    self.flats.append((symbol, timestamp, strategy))


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
) -> tuple[SignalProcessingService, FakePublisher, FakeNotifier]:
  publisher = FakePublisher()
  notifier = FakeNotifier()
  service = SignalProcessingService(
    signal_repository=FakeSignalRepository(return_id=signal_id),
    setting_repository=FakeSettingRepository(blocked=blocked),
    publisher=publisher,
    notifier=notifier,
    webhook_secret=secret,
  )
  return service, publisher, notifier


async def test_happy_path_publishes_and_notifies():
  service, publisher, notifier = _make_service()
  result = await service.process(_payload())

  assert result["status"] == "accepted"
  assert len(publisher.published) == 1
  assert publisher.published[0].symbol == "XAUUSD"
  assert len(notifier.messages) == 1


async def test_flat_uses_publish_flat():
  service, publisher, notifier = _make_service()
  result = await service.process(_payload(action=SignalActionEnum.FLAT))

  assert result["status"] == "accepted"
  assert publisher.flats == [
    ("XAUUSD", datetime(2026, 1, 1, tzinfo=timezone.utc), "strat")
  ]
  assert publisher.published == []


async def test_invalid_token_raises_401():
  service, _, _ = _make_service()
  with pytest.raises(SignalError) as exc:
    await service.process(_payload(token="wrong"))
  assert exc.value.status_code == 401


async def test_missing_secret_raises_500():
  service, _, _ = _make_service(secret="")
  with pytest.raises(SignalError) as exc:
    await service.process(_payload())
  assert exc.value.status_code == 500


async def test_blocked_raises_403_and_notifies():
  service, publisher, notifier = _make_service(blocked=True)
  with pytest.raises(SignalError) as exc:
    await service.process(_payload())
  assert exc.value.status_code == 403
  assert len(notifier.messages) == 1  # blocked notification
  assert publisher.published == []


async def test_persist_failure_raises_500():
  service, _, _ = _make_service(signal_id=None)
  with pytest.raises(SignalError) as exc:
    await service.process(_payload())
  assert exc.value.status_code == 500


# ── Notification timezone wiring ─────────────────────────────────────


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

  await service.process(_payload())

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

  await service.process(_payload(action=SignalActionEnum.FLAT))

  assert "Time: 2026-01-01 00:00:00 (UTC+0)" in notifier.messages[0]


async def test_signal_notification_defaults_to_utc_plus_7_when_unset():
  service, _, notifier = _make_service()
  await service.process(_payload())
  assert "Time: 2026-01-01 07:00:00 (UTC+7)" in notifier.messages[0]
