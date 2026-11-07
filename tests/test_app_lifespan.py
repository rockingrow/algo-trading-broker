"""The app lifespan: services stop in reverse start order, and a service that
fails to start unwinds everything started before it.

Every collaborator is replaced with a recorder, so this exercises only the
wiring in ``broker.app.lifespan`` — no NATS, no Postgres, no Telegram.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI

import broker.app as app_module


class Recorder:
  def __init__(self):
    self.events: list[str] = []
    self.fail_on_start: set[str] = set()


def _service_class(recorder: Recorder, name: str):
  class FakeService:
    def __init__(self, *args, **kwargs):
      pass

    async def start(self):
      if name in recorder.fail_on_start:
        raise RuntimeError(f"{name} failed to start")
      recorder.events.append(f"start:{name}")

    async def stop(self, *args, **kwargs):
      recorder.events.append(f"stop:{name}")

  FakeService.__name__ = name
  return FakeService


class FakeNatsClient:
  def __init__(self, recorder: Recorder):
    self._recorder = recorder

  def set_notifier(self, notifier):
    pass

  async def connect(self):
    self._recorder.events.append("start:nats")

  async def close(self):
    self._recorder.events.append("stop:nats")

  def subjects_line(self):
    return ""

  def listen_subjects_line(self):
    return ""


class FakeTelegram:
  def __init__(self, *args, **kwargs):
    pass

  async def send_message(self, text):
    return None


@pytest.fixture
def recorder(monkeypatch) -> Recorder:
  recorder = Recorder()

  async def init_db():
    recorder.events.append("start:db")

  async def close_db():
    recorder.events.append("stop:db")

  monkeypatch.setattr(app_module, "init_db", init_db)
  monkeypatch.setattr(app_module, "close_db", close_db)
  monkeypatch.setattr(app_module, "nats_client", FakeNatsClient(recorder))
  monkeypatch.setattr(app_module, "TelegramNotification", FakeTelegram)
  monkeypatch.setattr(app_module.settings.telegram, "LOG_ERRORS_ENABLED", False)

  queued_names = iter(["nats_notifier", "signals_notifier", "demo_notifier"])

  def queued_notifier(inner):
    return _service_class(recorder, next(queued_names))()

  monkeypatch.setattr(app_module, "QueuedNotifier", queued_notifier)
  monkeypatch.setattr(app_module, "make_signals_notifier", lambda repo: None)
  monkeypatch.setattr(app_module, "make_signal_broadcaster", lambda: None)
  monkeypatch.setattr(
    app_module,
    "make_broadcast_dispatcher",
    lambda repo: _service_class(recorder, "broadcast_dispatcher")(),
  )
  for name in (
    "TradeCardService",
    "DeferredEnqueuer",
    "TradeEventConsumer",
    "SystemEventConsumer",
    "DemoSignalConsumer",
    "SignalWorker",
    "SignalRetryJob",
  ):
    monkeypatch.setattr(app_module, name, _service_class(recorder, name))
  return recorder


async def test_services_stop_in_reverse_start_order(recorder):
  async with app_module.lifespan(FastAPI()):
    pass

  starts = [event[6:] for event in recorder.events if event.startswith("start:")]
  stops = [event[5:] for event in recorder.events if event.startswith("stop:")]
  assert stops == list(reversed(starts))
  # The contract the order encodes.
  assert stops.index("SignalWorker") < stops.index("broadcast_dispatcher")
  assert stops.index("TradeEventConsumer") < stops.index("TradeCardService")
  assert stops.index("DeferredEnqueuer") < stops.index("nats")
  assert stops[-1] == "db"


async def test_a_failed_start_unwinds_what_already_started(recorder):
  recorder.fail_on_start.add("SignalWorker")

  with pytest.raises(RuntimeError, match="SignalWorker failed to start"):
    async with app_module.lifespan(FastAPI()):
      pass

  started = [event[6:] for event in recorder.events if event.startswith("start:")]
  stopped = [event[5:] for event in recorder.events if event.startswith("stop:")]
  assert "SignalWorker" not in started
  assert stopped == list(reversed(started))
  assert "nats" in stopped and "db" in stopped
