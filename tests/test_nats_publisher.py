import json
from datetime import datetime, timezone

from broker.schemas.account_schema import MarketTypeEnum
from broker.schemas.core import SignalActionEnum
from broker.schemas.publisher_schema import (
  AdminActionEnum,
  PublishTopicEnum,
  SystemActionEnum,
  TradingSignal,
)
from broker.services.nats_service import NatsPublisher


class FakeNC:
  """Captures every publish() call as (subject, decoded-json-dict)."""

  def __init__(self):
    self.published: list[tuple[str, dict]] = []

  async def publish(self, subject, payload):
    self.published.append((subject, json.loads(payload.decode())))


class FakeAck:
  def __init__(self, seq: int = 1):
    self.seq = seq


class FakeJS:
  def __init__(self):
    self.published: list[tuple[str, dict]] = []

  async def publish(self, subject, payload):
    self.published.append((subject, json.loads(payload.decode())))
    return FakeAck(seq=len(self.published))


class FakeConn:
  def __init__(self):
    self.nc = FakeNC()
    self.js = FakeJS()


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
  await publisher.publish_flat(
    signal_id="sig-flat-1", symbol="XAUUSD", timestamp=ts, strategy="strat-x"
  )

  subject, body = conn.nc.published[0]
  assert subject == "strat-x"
  # signal_id is required so workers can de-duplicate a live FLAT against the
  # same signal replayed inside a SYSTEM.RETRY_SIGNALS bundle.
  assert body == {
    "signal_id": "sig-flat-1",
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
    market_type=MarketTypeEnum.FOREX,
    gateway="MT5",
  )

  subject, body = conn.nc.published[0]
  assert subject == PublishTopicEnum.ADMIN.value
  # use_enum_values=True means the action is serialised as its string value.
  assert body["action"] == "FLAT"
  assert body["account_id"] == "acc-1"
  assert body["market_type"] == "FOREX"
  assert body["gateway"] == "MT5"


async def test_publish_system_signal_to_system_subject():
  conn = FakeConn()
  publisher = NatsPublisher(connection=conn)
  await publisher.publish_system_signal(
    action=SystemActionEnum.CRYPTO_LEVERAGE_INIT,
    account_id="CRYPTO-BINANCE-7654321",
    symbols=["BTC", "ETH"],
    default_leverage=10,
  )

  subject, body = conn.nc.published[0]
  assert subject == PublishTopicEnum.SYSTEM.value
  assert body["action"] == "CRYPTO_LEVERAGE_INIT"
  assert body["account_id"] == "CRYPTO-BINANCE-7654321"
  assert body["symbols"] == ["BTC", "ETH"]
  assert body["default_leverage"] == 10


async def test_publish_system_signal_to_reply_inbox():
  conn = FakeConn()
  publisher = NatsPublisher(connection=conn)
  await publisher.publish_system_signal(
    action=SystemActionEnum.CRYPTO_LEVERAGE_INIT,
    account_id="CRYPTO-BINANCE-7654321",
    symbols=["BTC"],
    default_leverage=5,
    subject="_INBOX.reply",
  )

  subject, body = conn.nc.published[0]
  # A reply inbox is targeted directly instead of the shared SYSTEM subject.
  assert subject == "_INBOX.reply"
  assert body["action"] == "CRYPTO_LEVERAGE_INIT"


async def test_publish_system_ack():
  conn = FakeConn()
  publisher = NatsPublisher(connection=conn)
  await publisher.publish_system_ack(subject="_INBOX.ack", account_id="FOREX-MT5-1")

  subject, body = conn.nc.published[0]
  assert subject == "_INBOX.ack"
  assert body["action"] == "WORKER_CONNECTED_ACK"
  assert body["account_id"] == "FOREX-MT5-1"


async def test_publish_system_error():
  conn = FakeConn()
  publisher = NatsPublisher(connection=conn)
  await publisher.publish_system_error(
    subject="_INBOX.err", account_id=None, reason="crypto settings not configured"
  )

  subject, body = conn.nc.published[0]
  assert subject == "_INBOX.err"
  assert body["action"] == "WORKER_CONNECTED_ERROR"
  assert body["account_id"] is None
  assert body["reason"] == "crypto settings not configured"


async def test_publish_webhook_event_targets_jetstream_signal_subject():
  conn = FakeConn()
  publisher = NatsPublisher(connection=conn)
  await publisher.publish_webhook_event(
    signal_id="sig-123",
    strategy="wt_cross_v1",
    envelope={"signal_id": "sig-123", "payload": {"strategy": "wt_cross_v1"}},
  )

  # Core NATS is untouched; the envelope lands on JetStream under SIGNALS.<strategy>.
  assert conn.nc.published == []
  assert len(conn.js.published) == 1
  subject, body = conn.js.published[0]
  assert subject == "SIGNALS.wt_cross_v1"
  assert body["signal_id"] == "sig-123"


async def test_publish_system_retry_signal_broadcasts_when_no_subject():
  conn = FakeConn()
  publisher = NatsPublisher(connection=conn)
  signal = _signal(strategy="wt_cross_v1")
  await publisher.publish_system_retry_signal(
    account_id="FOREX-MT5-1", signals=[signal]
  )

  subject, body = conn.nc.published[0]
  assert subject == PublishTopicEnum.SYSTEM.value
  assert body["action"] == SystemActionEnum.RETRY_SIGNALS.value
  assert body["account_id"] == "FOREX-MT5-1"
  assert len(body["signals"]) == 1
  assert body["signals"][0]["signal_id"] == "sig-1"


async def test_publish_system_retry_signal_replies_directly_when_subject_set():
  conn = FakeConn()
  publisher = NatsPublisher(connection=conn)
  await publisher.publish_system_retry_signal(
    account_id="FOREX-MT5-1", signals=[], subject="_INBOX.reply"
  )
  subject, body = conn.nc.published[0]
  assert subject == "_INBOX.reply"
  assert body["action"] == SystemActionEnum.RETRY_SIGNALS.value
  assert body["signals"] == []
