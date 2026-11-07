import json

import pytest

from broker.schemas.trade_event_schema import PositionEvent
from broker.services.nats_service import TradeEventConsumer, TradeOutcome


class FakeTradeRepo:
  def __init__(self, raise_exc: bool = False):
    self.events: list[PositionEvent] = []
    self.raise_exc = raise_exc

  async def upsert_by_position_event(self, event):
    if self.raise_exc:
      raise RuntimeError("boom")
    self.events.append(event)
    return None

  async def list_by_account(self, *a, **k):
    return []

  async def count_by_account(self, *a, **k):
    return 0


class FakeMsg:
  def __init__(self, data: bytes):
    self.data = data


def _valid_event_dict() -> dict:
  return {
    "event": "CREATED",
    "market": "FOREX",
    "strategy": "strat",
    "id": 1,
    "ref_source_id": "rs-1",
    "ref_id": "r-1",
    "symbol": "XAUUSD",
    "action": "LONG",
    "volume": 0.1,
    "opened_price": 100.0,
    "status": "OPENED",
    "account_id": "acc-1",
  }


async def test_valid_event_is_forwarded_to_repository():
  repo = FakeTradeRepo()
  consumer = TradeEventConsumer(trade_repository=repo)
  await consumer.handle_subject_trade(FakeMsg(json.dumps(_valid_event_dict()).encode()))

  assert len(repo.events) == 1
  assert repo.events[0].account_id == "acc-1"
  assert repo.events[0].symbol == "XAUUSD"


async def test_malformed_json_is_swallowed():
  repo = FakeTradeRepo()
  consumer = TradeEventConsumer(trade_repository=repo)
  await consumer.handle_subject_trade(FakeMsg(b"{not-json"))
  assert repo.events == []


async def test_invalid_schema_is_swallowed():
  repo = FakeTradeRepo()
  consumer = TradeEventConsumer(trade_repository=repo)
  # Missing required fields like account_id / status.
  await consumer.handle_subject_trade(
    FakeMsg(json.dumps({"event": "CREATED"}).encode())
  )
  assert repo.events == []


async def test_repository_exception_does_not_propagate():
  repo = FakeTradeRepo(raise_exc=True)
  consumer = TradeEventConsumer(trade_repository=repo)
  # Should log and report a retry rather than raise.
  outcome = await consumer.handle_subject_trade(
    FakeMsg(json.dumps(_valid_event_dict()).encode())
  )
  assert outcome is TradeOutcome.RETRY


class FakeCardService:
  def __init__(self, raise_exc: bool = False):
    self.calls: list[tuple] = []
    self.raise_exc = raise_exc

  async def handle_event(self, event, trade):
    if self.raise_exc:
      raise RuntimeError("card boom")
    self.calls.append((event, trade))


async def test_card_service_invoked_with_persisted_trade():
  repo = FakeTradeRepo()
  cards = FakeCardService()
  consumer = TradeEventConsumer(trade_repository=repo, card_service=cards)
  await consumer.handle_subject_trade(FakeMsg(json.dumps(_valid_event_dict()).encode()))
  assert len(cards.calls) == 1
  event, _trade = cards.calls[0]
  assert event.account_id == "acc-1"


async def test_card_service_skipped_when_persist_fails():
  repo = FakeTradeRepo(raise_exc=True)
  cards = FakeCardService()
  consumer = TradeEventConsumer(trade_repository=repo, card_service=cards)
  await consumer.handle_subject_trade(FakeMsg(json.dumps(_valid_event_dict()).encode()))
  assert cards.calls == []


async def test_card_service_exception_does_not_propagate():
  repo = FakeTradeRepo()
  cards = FakeCardService(raise_exc=True)
  consumer = TradeEventConsumer(trade_repository=repo, card_service=cards)
  # A card-delivery failure must not bubble out of TRADE consumption.
  await consumer.handle_subject_trade(FakeMsg(json.dumps(_valid_event_dict()).encode()))


class FakeSubscription:
  def __init__(self):
    self.unsubscribed = False

  async def unsubscribe(self):
    self.unsubscribed = True

  async def fetch(self, batch, timeout=None):
    raise TimeoutError


class FakeJetStream:
  def __init__(self):
    self.pull_subscribed: dict | None = None
    self._sub = FakeSubscription()

  async def pull_subscribe(self, *, subject, durable, stream):
    self.pull_subscribed = {"subject": subject, "durable": durable, "stream": stream}
    return self._sub


class FakeConn:
  def __init__(self):
    self.js = FakeJetStream()


async def test_start_pulls_from_the_trade_stream():
  repo = FakeTradeRepo()
  conn = FakeConn()
  consumer = TradeEventConsumer(trade_repository=repo, connection=conn)
  await consumer.start()
  try:
    assert conn.js.pull_subscribed == {
      "subject": "TRADE",
      "durable": "broker_trade_handler",
      "stream": "TRADES",
    }
  finally:
    await consumer.stop()


async def test_stop_unsubscribes():
  repo = FakeTradeRepo()
  conn = FakeConn()
  consumer = TradeEventConsumer(trade_repository=repo, connection=conn)
  await consumer.start()
  await consumer.stop()
  assert conn.js._sub.unsubscribed is True


async def test_stop_without_start_is_noop():
  repo = FakeTradeRepo()
  consumer = TradeEventConsumer(trade_repository=repo, connection=FakeConn())
  # No subscription yet; should not raise.
  await consumer.stop()


# ── Settling: a report is acked only once it is persisted ───────────


class SettlingMsg(FakeMsg):
  def __init__(self, data: bytes):
    super().__init__(data)
    self.settled: list[tuple] = []

  async def ack(self):
    self.settled.append(("ack",))

  async def nak(self, delay=None):
    self.settled.append(("nak", delay))

  async def term(self):
    self.settled.append(("term",))


async def test_persisted_report_is_acked():
  consumer = TradeEventConsumer(trade_repository=FakeTradeRepo())
  msg = SettlingMsg(json.dumps(_valid_event_dict()).encode())
  await consumer._handle_one(msg)
  assert msg.settled == [("ack",)]


async def test_report_is_naked_with_delay_when_the_write_fails():
  from broker.services.jetstream_service import NAK_DELAY_SECONDS

  consumer = TradeEventConsumer(trade_repository=FakeTradeRepo(raise_exc=True))
  msg = SettlingMsg(json.dumps(_valid_event_dict()).encode())
  await consumer._handle_one(msg)
  # Left in the stream for redelivery instead of being dropped.
  assert msg.settled == [("nak", NAK_DELAY_SECONDS)]


async def test_unreadable_report_is_terminated():
  consumer = TradeEventConsumer(trade_repository=FakeTradeRepo())
  msg = SettlingMsg(b"{not-json")
  await consumer._handle_one(msg)
  assert msg.settled == [("term",)]


async def test_card_failure_still_acks_the_persisted_report():
  consumer = TradeEventConsumer(
    trade_repository=FakeTradeRepo(), card_service=FakeCardService(raise_exc=True)
  )
  msg = SettlingMsg(json.dumps(_valid_event_dict()).encode())
  await consumer._handle_one(msg)
  assert msg.settled == [("ack",)]


@pytest.mark.parametrize(
  "raw", [b"[]", b'"x"', b"\xff\xfe"], ids=["array", "string", "invalid-utf8"]
)
async def test_a_report_that_is_not_an_object_is_terminated(raw):
  consumer = TradeEventConsumer(trade_repository=FakeTradeRepo())
  msg = SettlingMsg(raw)
  await consumer._handle_one(msg)
  assert msg.settled == [("term",)]


async def test_a_report_failing_for_too_long_is_given_up_on():
  from types import SimpleNamespace

  from broker.services.nats_service import TRADE_MAX_DELIVERIES

  consumer = TradeEventConsumer(trade_repository=FakeTradeRepo(raise_exc=True))
  msg = SettlingMsg(json.dumps(_valid_event_dict()).encode())
  msg.metadata = SimpleNamespace(num_delivered=TRADE_MAX_DELIVERIES)
  await consumer._handle_one(msg)
  assert msg.settled == [("term",)]


class BrokenJetStream:
  async def pull_subscribe(self, **kwargs):
    from nats.js.errors import NotFoundError

    raise NotFoundError()


class CoreNC:
  def __init__(self):
    self.subscribed: list[str] = []
    self.sub = FakeSubscription()

  async def subscribe(self, subject, cb):
    self.subscribed.append(subject)
    return self.sub


class ConnWithoutStream:
  def __init__(self):
    self.js = BrokenJetStream()
    self.nc = CoreNC()


async def test_start_falls_back_to_core_nats_when_the_stream_is_unusable():
  conn = ConnWithoutStream()
  consumer = TradeEventConsumer(trade_repository=FakeTradeRepo(), connection=conn)
  # The broker must still start: TRADE durability is not a precondition.
  await consumer.start()
  assert conn.nc.subscribed == ["TRADE"]
  await consumer.stop()
  assert conn.nc.sub.unsubscribed is True
