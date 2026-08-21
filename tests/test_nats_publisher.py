import json
from datetime import datetime, timezone

import pytest

from broker.schemas.account_schema import AccountSettings, MarketTypeEnum
from broker.schemas.core import SignalActionEnum
from broker.schemas.publisher_schema import (
  AdminActionEnum,
  CryptoLeverageConfig,
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
    self.calls: list[dict] = []

  async def publish(self, subject, payload, timeout=None, headers=None):
    self.published.append((subject, json.loads(payload.decode())))
    self.calls.append({"timeout": timeout, "headers": headers})
    return FakeAck(seq=len(self.published))


class FakeConn:
  def __init__(self, is_connected: bool = True):
    self.nc = FakeNC()
    self.js = FakeJS()
    self.is_connected = is_connected


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
    signal_id="sig-flat-1",
    signal_uxid="9f2c4b7e18a3d605",
    symbol="XAUUSD",
    timestamp=ts,
    strategy="strat-x",
  )

  subject, body = conn.nc.published[0]
  assert subject == "strat-x"
  # signal_id is required so workers can de-duplicate a live FLAT against the
  # same signal replayed inside a WORKER_CONNECTED_ACK's retry_signals;
  # signal_uxid names the trade cycle being closed.
  assert body == {
    "signal_id": "sig-flat-1",
    "signal_uxid": "9f2c4b7e18a3d605",
    "strategy": "strat-x",
    "timestamp": ts.isoformat(),
    "action": SignalActionEnum.FLAT.value,
    "symbol": "XAUUSD",
  }


async def test_publish_admin_signal_account_scoped_uses_private_subject():
  conn = FakeConn()
  publisher = NatsPublisher(connection=conn)
  await publisher.publish_admin_signal(
    action=AdminActionEnum.FLAT,
    strategy="s",
    symbol="XAUUSD",
    account_id="acc-1",
    market=MarketTypeEnum.FOREX,
    gateway="MT5",
  )

  subject, body = conn.nc.published[0]
  # Account-scoped admin actions go to the per-account private subject so no
  # other worker learns the account_id.
  assert subject == "ADMIN.FOREX.MT5.acc-1"
  # use_enum_values=True means the action is serialised as its string value.
  assert body["action"] == "FLAT"
  assert body["account_id"] == "acc-1"
  assert body["market"] == "FOREX"
  assert body["gateway"] == "MT5"


async def test_publish_admin_signal_broadcast_uses_shared_subject():
  conn = FakeConn()
  publisher = NatsPublisher(connection=conn)
  await publisher.publish_admin_signal(
    action=AdminActionEnum.FLAT,
    strategy="s",
    symbol="XAUUSD",
  )

  subject, body = conn.nc.published[0]
  # No account_id -> broadcast on the shared ADMIN subject for every worker.
  assert subject == PublishTopicEnum.ADMIN.value
  assert body["action"] == "FLAT"
  assert body["account_id"] is None


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
  # The configuration blocks are always present, empty/defaulted when there is
  # nothing to send, so a worker can parse them unconditionally.
  assert body["strategy_magic_map"] == {}
  assert body["retry_signals"] == []
  assert body["settings"] == {"signal_blocked": False}
  assert body["crypto_leverage_init"] is None


async def test_publish_system_ack_carries_the_whole_handshake_config():
  conn = FakeConn()
  publisher = NatsPublisher(connection=conn)
  await publisher.publish_system_ack(
    subject="_INBOX.ack",
    account_id="CRYPTO-BINANCE-7654321",
    strategy_magic_map={"MT5_GOLD_M5_V1": 20260409},
    retry_signals=[_signal(strategy="MT5_GOLD_M5_V1")],
    settings=AccountSettings(signal_blocked=True),
    crypto_leverage_init=CryptoLeverageConfig(
      symbols=["BTC", "ETH"], default_leverage=10
    ),
  )

  # Everything the handshake used to send as separate messages fits in the one
  # message a reply inbox accepts.
  assert len(conn.nc.published) == 1
  subject, body = conn.nc.published[0]
  assert subject == "_INBOX.ack"
  assert body["action"] == "WORKER_CONNECTED_ACK"
  assert body["strategy_magic_map"] == {"MT5_GOLD_M5_V1": 20260409}
  assert len(body["retry_signals"]) == 1
  assert body["retry_signals"][0]["signal_id"] == "sig-1"
  assert body["settings"] == {"signal_blocked": True}
  assert body["crypto_leverage_init"] == {
    "symbols": ["BTC", "ETH"],
    "default_leverage": 10,
  }


async def test_publish_system_ack_broadcasts_when_no_subject():
  conn = FakeConn()
  publisher = NatsPublisher(connection=conn)
  await publisher.publish_system_ack(
    account_id="CRYPTO-BINANCE-7654321",
    strategy_magic_map={"MT5_GOLD_M5_V1": 20260409},
  )

  subject, body = conn.nc.published[0]
  # No subject (fire-and-forget worker) → falls back to the shared SYSTEM
  # subject, still carrying account_id so the worker can filter for itself.
  assert subject == PublishTopicEnum.SYSTEM.value
  assert body["action"] == "WORKER_CONNECTED_ACK"
  assert body["account_id"] == "CRYPTO-BINANCE-7654321"


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


async def test_publish_webhook_event_forwards_deadline_and_dedup_id():
  conn = FakeConn()
  publisher = NatsPublisher(connection=conn)
  await publisher.publish_webhook_event(
    signal_id="",
    strategy="wt_cross_v1",
    envelope={"payload": {"strategy": "wt_cross_v1"}},
    timeout=0.75,
    msg_id="abc123",
  )

  # The caller's deadline bounds the PubAck wait (nats-py would wait 5s), and
  # the id lets JetStream drop a retry of an envelope it already stored.
  assert conn.js.calls[0]["timeout"] == 0.75
  assert conn.js.calls[0]["headers"] == {"Nats-Msg-Id": "abc123"}


async def test_publish_webhook_event_fails_fast_while_disconnected():
  conn = FakeConn(is_connected=False)
  publisher = NatsPublisher(connection=conn)

  # Buffering the write and waiting out the timeout for an ack that cannot
  # arrive is exactly what costs TradingView its delivery.
  with pytest.raises(ConnectionError):
    await publisher.publish_webhook_event(
      signal_id="", strategy="wt_cross_v1", envelope={"payload": {}}
    )
  assert conn.js.published == []


async def test_replayed_signals_keep_the_live_signal_shape():
  conn = FakeConn()
  publisher = NatsPublisher(connection=conn)
  await publisher.publish_system_ack(
    subject="_INBOX.reply",
    account_id="FOREX-MT5-1",
    retry_signals=[_signal(strategy="wt_cross_v1")],
  )

  _subject, body = conn.nc.published[0]
  replayed = body["retry_signals"][0]
  # Identical to what the worker sees live on the strategy subject, so it can
  # run the replay through the same handler and de-duplicate by signal_id.
  assert replayed["signal_id"] == "sig-1"
  assert replayed["strategy"] == "wt_cross_v1"
  assert replayed["action"] == SignalActionEnum.LONG.value
  assert replayed["symbol"] == "XAUUSD"
