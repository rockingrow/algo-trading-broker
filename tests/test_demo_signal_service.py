"""The drill subject: Telegram gets the signal, no worker ever does.

The assertions that matter here are the negative ones. ``DemoSignalConsumer``
is constructed without a publisher, so these tests cannot check "it did not
publish" by inspecting a fake publisher — they check the two ways a drill could
still escape to a worker: the subject it subscribes to overlapping the
JetStream buffer's filter, and a drill being written to the ``signals`` table
that the ``WORKER_CONNECTED_ACK`` handshake replays.
"""

import inspect
import json

from broker.nats import (
  DEMO_SIGNAL_SUBJECT_FILTER,
  DEMO_SIGNAL_SUBJECT_PREFIX,
  JETSTREAM_SIGNAL_SUBJECT_FILTER,
  JETSTREAM_SIGNAL_SUBJECT_PREFIX,
)
from broker.services.demo_signal_service import DemoSignalConsumer


class FakeMsg:
  def __init__(self, data: bytes, subject: str = "SIGNALS_DEMO.wt_cross_v1"):
    self.data = data
    self.subject = subject


class FakeSubscription:
  def __init__(self):
    self.unsubscribed = False

  async def unsubscribe(self):
    self.unsubscribed = True


class FakeNC:
  def __init__(self):
    self.subscriptions: list[str] = []

  async def subscribe(self, subject, cb=None):
    self.subscriptions.append(subject)
    return FakeSubscription()


class FakeConn:
  def __init__(self):
    self.nc = FakeNC()


class FakeBroadcaster:
  def __init__(self, raise_exc: Exception | None = None):
    self.recorded: list[dict] = []
    self._raise = raise_exc

  async def broadcast(self, payload, *, attempt_number=None):
    if self._raise is not None:
      raise self._raise
    self.recorded.append({"payload": payload, "attempt_number": attempt_number})


class FakeNotifier:
  def __init__(self):
    self.messages: list[str] = []

  async def send_message(self, message: str):
    self.messages.append(message)


def _envelope(**overrides) -> bytes:
  position = {"action": "LONG", "price": 2400.0, "quantity": 0.41, "sl": 2388.0}
  payload = {
    "strategy": "EXAMPLE_GOLD_M15_V1",
    "symbol": "XAUUSD",
    "timeframe": "15",
    "timestamp": "2026-06-30T00:00:00+00:00",
    "signal_uxid": "A1B2C3D4E5F60718",
    "position": position,
    "token": "secret",
  }
  payload.update(overrides)
  return json.dumps({"payload": payload}).encode()


def _consumer(**kwargs) -> tuple[DemoSignalConsumer, FakeBroadcaster, FakeNotifier]:
  broadcaster = kwargs.pop("broadcaster", None) or FakeBroadcaster()
  notifier = kwargs.pop("notifier", None) or FakeNotifier()
  consumer = DemoSignalConsumer(
    broadcaster=broadcaster,
    notifier=notifier,
    connection=FakeConn(),
    **kwargs,
  )
  return consumer, broadcaster, notifier


# ── The safety properties ──────────────────────────────────────────────


def test_demo_subject_cannot_match_the_jetstream_signal_filter():
  """``SIGNALS_DEMO`` is a different first token, not a child of ``SIGNALS``.

  This is the whole reason a drill cannot reach a worker: if the demo subject
  were ``SIGNALS.DEMO.*`` it would be captured by the stream ``SignalWorker``
  drains and fanned out like any other signal.
  """
  assert DEMO_SIGNAL_SUBJECT_PREFIX != JETSTREAM_SIGNAL_SUBJECT_PREFIX
  assert not DEMO_SIGNAL_SUBJECT_PREFIX.startswith(f"{JETSTREAM_SIGNAL_SUBJECT_PREFIX}.")
  # The JetStream filter's wildcard only follows its first token, so the demo
  # prefix matching it would require the two first tokens to be equal.
  stream_root = JETSTREAM_SIGNAL_SUBJECT_FILTER.split(".")[0]
  assert DEMO_SIGNAL_SUBJECT_FILTER.split(".")[0] != stream_root


def test_consumer_takes_neither_a_publisher_nor_a_signal_repository():
  """The two collaborators that could let a drill escape are not accepted at all.

  A publisher is how a signal reaches a worker directly; a signal repository is
  how it reaches one indirectly, because ``WORKER_CONNECTED_ACK`` replays every
  recently persisted signal into ``retry_signals``. Keeping both out of the
  constructor makes "a drill commands nothing" unfalsifiable by a later edit
  that only rewires the app, instead of a branch someone has to get right.
  """
  accepted = set(inspect.signature(DemoSignalConsumer.__init__).parameters)
  assert accepted == {"self", "broadcaster", "notifier", "connection"}
  held = set(vars(_consumer()[0]))
  assert not any("publish" in name or "repo" in name for name in held)


async def test_start_subscribes_to_the_demo_filter_only():
  consumer, _, _ = _consumer()
  await consumer.start()
  assert consumer._conn.nc.subscriptions == [DEMO_SIGNAL_SUBJECT_FILTER]


# ── The happy path ─────────────────────────────────────────────────────


async def test_valid_envelope_is_broadcast_and_announced():
  consumer, broadcaster, notifier = _consumer()
  await consumer.handle_demo_signal(FakeMsg(_envelope()))

  assert len(broadcaster.recorded) == 1
  recorded = broadcaster.recorded[0]["payload"]
  assert recorded.strategy == "EXAMPLE_GOLD_M15_V1"
  assert recorded.symbol == "XAUUSD"
  assert recorded.signal_uxid == "A1B2C3D4E5F60718"
  # No attempt marker: a drill is never retried, so the card must not grow an
  # "Attempt: N" line that would make it read like a redelivery.
  assert broadcaster.recorded[0]["attempt_number"] is None

  assert len(notifier.messages) == 1
  message = notifier.messages[0]
  assert "Demo signal received" in message
  assert "EXAMPLE_GOLD_M15_V1" in message
  assert "worker" in message


async def test_cycle_id_reaches_the_broadcaster_unchanged():
  """The engine mints the cycle id; a drill must not be regrouped under a new one."""
  consumer, broadcaster, _ = _consumer()
  await consumer.handle_demo_signal(FakeMsg(_envelope(signal_uxid="0F1E2D3C4B5A6978")))
  assert broadcaster.recorded[0]["payload"].signal_uxid == "0F1E2D3C4B5A6978"


# ── Failure paths ──────────────────────────────────────────────────────


async def test_malformed_json_is_dropped_without_raising():
  consumer, broadcaster, notifier = _consumer()
  await consumer.handle_demo_signal(FakeMsg(b"{not-json"))
  assert broadcaster.recorded == []
  assert notifier.messages == []


async def test_envelope_without_payload_is_dropped():
  consumer, broadcaster, notifier = _consumer()
  await consumer.handle_demo_signal(FakeMsg(json.dumps({"nope": 1}).encode()))
  assert broadcaster.recorded == []
  assert notifier.messages == []


async def test_invalid_payload_is_dropped():
  """A payload the webhook would reject is the finding the drill exists for."""
  consumer, broadcaster, notifier = _consumer()
  await consumer.handle_demo_signal(FakeMsg(_envelope(position={"action": "NOPE"})))
  assert broadcaster.recorded == []
  assert notifier.messages == []


async def test_the_cycle_is_recorded_before_telegram_is_called():
  """Order matters: the card must be on the write log before any HTTP call.

  nats-py awaits this callback before pulling the subscription's next message,
  so a notifier that stalls would otherwise hold up the *next* drill's DB write
  as well as its own send. Recording first keeps the dispatcher — which
  delivers off that write log — independent of Telegram's pace.
  """
  order: list[str] = []

  class OrderedBroadcaster(FakeBroadcaster):
    async def broadcast(self, payload, *, attempt_number=None):
      order.append("broadcast")
      await super().broadcast(payload, attempt_number=attempt_number)

  class OrderedNotifier(FakeNotifier):
    async def send_message(self, message: str):
      order.append("notify")
      await super().send_message(message)

  consumer = DemoSignalConsumer(
    broadcaster=OrderedBroadcaster(),
    notifier=OrderedNotifier(),
    connection=FakeConn(),
  )
  await consumer.handle_demo_signal(FakeMsg(_envelope()))
  assert order == ["broadcast", "notify"]


async def test_broadcast_failure_still_announces_to_the_operator():
  """Telegram rendering and the operator line fail independently."""
  consumer, _, notifier = _consumer(broadcaster=FakeBroadcaster(raise_exc=RuntimeError("db")))
  await consumer.handle_demo_signal(FakeMsg(_envelope()))
  assert len(notifier.messages) == 1


async def test_no_broadcaster_and_no_notifier_is_harmless():
  consumer = DemoSignalConsumer(connection=FakeConn())
  await consumer.handle_demo_signal(FakeMsg(_envelope()))


async def test_stop_before_start_is_harmless():
  consumer, _, _ = _consumer()
  await consumer.stop()


async def test_stop_unsubscribes():
  consumer, _, _ = _consumer()
  await consumer.start()
  subscription = consumer._sub
  await consumer.stop()
  assert subscription.unsubscribed is True
  assert consumer._sub is None
