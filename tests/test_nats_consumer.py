import json

from broker.schemas.trade_event_schema import PositionEvent
from broker.services.nats_service import TradeEventConsumer


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
  # Should log and return rather than raise.
  await consumer.handle_subject_trade(FakeMsg(json.dumps(_valid_event_dict()).encode()))


class FakeBroadcast:
  def __init__(self, raise_exc: bool = False):
    self.calls: list[tuple] = []
    self.raise_exc = raise_exc

  async def maybe_broadcast(self, event, trade):
    if self.raise_exc:
      raise RuntimeError("broadcast boom")
    self.calls.append((event, trade))


async def test_broadcast_service_invoked_with_persisted_trade():
  repo = FakeTradeRepo()
  broadcast = FakeBroadcast()
  consumer = TradeEventConsumer(trade_repository=repo, broadcast_service=broadcast)
  await consumer.handle_subject_trade(FakeMsg(json.dumps(_valid_event_dict()).encode()))
  assert len(broadcast.calls) == 1
  event, _trade = broadcast.calls[0]
  assert event.account_id == "acc-1"


async def test_broadcast_skipped_when_persist_fails():
  repo = FakeTradeRepo(raise_exc=True)
  broadcast = FakeBroadcast()
  consumer = TradeEventConsumer(trade_repository=repo, broadcast_service=broadcast)
  await consumer.handle_subject_trade(FakeMsg(json.dumps(_valid_event_dict()).encode()))
  assert broadcast.calls == []


async def test_broadcast_exception_does_not_propagate():
  repo = FakeTradeRepo()
  broadcast = FakeBroadcast(raise_exc=True)
  consumer = TradeEventConsumer(trade_repository=repo, broadcast_service=broadcast)
  # A broadcast failure must not bubble out of TRADE consumption.
  await consumer.handle_subject_trade(FakeMsg(json.dumps(_valid_event_dict()).encode()))


class FakeSubscription:
  def __init__(self):
    self.unsubscribed = False

  async def unsubscribe(self):
    self.unsubscribed = True


class FakeConnNC:
  def __init__(self):
    self.subscribed_to = None
    self._sub = FakeSubscription()

  async def subscribe(self, subject, cb):
    self.subscribed_to = subject
    return self._sub


class FakeConn:
  def __init__(self):
    self.nc = FakeConnNC()

  class LISTEN_SUBJECT:
    value = "TRADE"


async def test_start_subscribes_to_listen_subject():
  repo = FakeTradeRepo()
  conn = FakeConn()
  consumer = TradeEventConsumer(trade_repository=repo, connection=conn)
  await consumer.start()
  assert conn.nc.subscribed_to == "TRADE"


async def test_stop_unsubscribes():
  repo = FakeTradeRepo()
  conn = FakeConn()
  consumer = TradeEventConsumer(trade_repository=repo, connection=conn)
  await consumer.start()
  await consumer.stop()
  assert conn.nc._sub.unsubscribed is True


async def test_stop_without_start_is_noop():
  repo = FakeTradeRepo()
  consumer = TradeEventConsumer(trade_repository=repo, connection=FakeConn())
  # No subscription yet; should not raise.
  await consumer.stop()
