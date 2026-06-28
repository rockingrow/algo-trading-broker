import json
from datetime import datetime, timezone

from broker.schemas.core import SignalActionEnum
from broker.schemas.publisher_schema import (
  AdminActionEnum,
  PublishTopicEnum,
  TradingSignal,
)
from broker.services.nats_publisher import NatsPublisher


class FakeNC:
  """Captures every publish() call as (subject, decoded-json-dict)."""

  def __init__(self):
    self.published: list[tuple[str, dict]] = []

  async def publish(self, subject, payload):
    self.published.append((subject, json.loads(payload.decode())))


class FakeConn:
  def __init__(self):
    self.nc = FakeNC()


def _signal(**overrides) -> TradingSignal:
  base = dict(
    signal_id="sig-1",
    strategy="my-strat",
    action=SignalActionEnum.LONG,
    symbol="XAUUSD",
    price=100.0,
    quantity=1.0,
  )
  base.update(overrides)
  return TradingSignal(**base)


async def test_publish_uses_strategy_subject_and_serialises_signal():
  conn = FakeConn()
  publisher = NatsPublisher(connection=conn)
  await publisher.publish(_signal())

  assert len(conn.nc.published) == 1
  subject, body = conn.nc.published[0]
  assert subject == "my-strat"
  assert body["signal_id"] == "sig-1"
  assert body["symbol"] == "XAUUSD"
  assert body["action"] == "LONG"


async def test_publish_none_is_skipped():
  conn = FakeConn()
  publisher = NatsPublisher(connection=conn)
  await publisher.publish(None)
  assert conn.nc.published == []


async def test_publish_flat_payload_shape():
  conn = FakeConn()
  publisher = NatsPublisher(connection=conn)
  ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
  await publisher.publish_flat(symbol="XAUUSD", timestamp=ts, strategy="strat-x")

  subject, body = conn.nc.published[0]
  assert subject == "strat-x"
  assert body == {
    "strategy": "strat-x",
    "timestamp": ts.isoformat(),
    "action": SignalActionEnum.FLAT.value,
    "symbol": "XAUUSD",
  }


async def test_publish_admin_signal_to_admin_subject():
  conn = FakeConn()
  publisher = NatsPublisher(connection=conn)
  await publisher.publish_admin_signal(
    action=AdminActionEnum.FLAT,
    strategy="s",
    symbol="XAUUSD",
    account_id="acc-1",
  )

  subject, body = conn.nc.published[0]
  assert subject == PublishTopicEnum.ADMIN.value
  # use_enum_values=True means the action is serialised as its string value.
  assert body["action"] == "FLAT"
  assert body["account_id"] == "acc-1"
