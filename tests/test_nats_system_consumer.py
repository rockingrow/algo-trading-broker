import json

from broker.schemas.publisher_schema import PublishTopicEnum, SystemActionEnum
from broker.services.nats_system_consumer import (
  CRYPTO_ALLOWED_SYMBOL_KEY,
  CRYPTO_MAX_LEVERAGE_KEY,
  SystemEventConsumer,
)


class FakeSettingRepo:
  def __init__(self, values: dict[str, str | None] | None = None):
    self._values = values or {}

  async def get(self, key: str) -> str | None:
    return self._values.get(key)

  async def set(self, key: str, value: str) -> bool:
    self._values[key] = value
    return True


class FakePublisher:
  def __init__(self):
    self.calls: list[dict] = []

  async def publish(self, signal) -> None:
    return None

  async def publish_flat(self, **kwargs) -> None:
    return None

  async def publish_admin_signal(self, **kwargs) -> None:
    return None

  async def publish_system_signal(self, **kwargs) -> None:
    self.calls.append(kwargs)


class FakeMsg:
  def __init__(self, data: bytes):
    self.data = data


def _worker_connected_payload(
  account_id: str = "CRYPTO-BINANCE-7654321",
  market: str = "CRYPTO",
  gateway: str = "BINANCE",
) -> bytes:
  return json.dumps(
    {
      "action": "WORKER_CONNECTED",
      "account_id": account_id,
      "timestamp": "2026-06-30T00:00:00+00:00",
      "market": market,
      "gateway": gateway,
    }
  ).encode()


def _make_consumer(
  settings: dict[str, str | None] | None = None,
) -> tuple[SystemEventConsumer, FakeSettingRepo, FakePublisher]:
  repo = FakeSettingRepo(
    settings
    if settings is not None
    else {
      CRYPTO_ALLOWED_SYMBOL_KEY: "BTC,ETH",
      CRYPTO_MAX_LEVERAGE_KEY: "10",
    }
  )
  publisher = FakePublisher()
  consumer = SystemEventConsumer(setting_repository=repo, publisher=publisher)
  return consumer, repo, publisher


async def test_worker_connected_publishes_crypto_leverage_init():
  consumer, _repo, publisher = _make_consumer()
  await consumer.handle_subject_system(FakeMsg(_worker_connected_payload()))

  assert len(publisher.calls) == 1
  call = publisher.calls[0]
  assert call["action"] == SystemActionEnum.CRYPTO_LEVERAGE_INIT
  assert call["account_id"] == "CRYPTO-BINANCE-7654321"
  assert call["symbols"] == ["BTC", "ETH"]
  assert call["default_leverage"] == 10


async def test_crypto_leverage_init_is_ignored():
  consumer, _repo, publisher = _make_consumer()
  payload = json.dumps(
    {
      "action": "CRYPTO_LEVERAGE_INIT",
      "account_id": "CRYPTO-BINANCE-7654321",
      "timestamp": "2026-06-30T00:00:00+00:00",
      "symbols": ["BTC", "ETH"],
      "default_leverage": 10,
    }
  ).encode()
  await consumer.handle_subject_system(FakeMsg(payload))
  assert publisher.calls == []


async def test_malformed_json_is_swallowed():
  consumer, _repo, publisher = _make_consumer()
  await consumer.handle_subject_system(FakeMsg(b"{not-json"))
  assert publisher.calls == []


async def test_invalid_schema_is_swallowed():
  consumer, _repo, publisher = _make_consumer()
  await consumer.handle_subject_system(
    FakeMsg(json.dumps({"action": "WORKER_CONNECTED"}).encode())
  )
  assert publisher.calls == []


async def test_worker_connected_missing_account_id_is_rejected():
  consumer, _repo, publisher = _make_consumer()
  await consumer.handle_subject_system(
    FakeMsg(
      json.dumps(
        {
          "action": "WORKER_CONNECTED",
          "market": "CRYPTO",
          "gateway": "BINANCE",
        }
      ).encode()
    )
  )
  assert publisher.calls == []


async def test_worker_connected_missing_market_is_rejected():
  consumer, _repo, publisher = _make_consumer()
  await consumer.handle_subject_system(
    FakeMsg(
      json.dumps(
        {
          "action": "WORKER_CONNECTED",
          "account_id": "CRYPTO-BINANCE-7654321",
          "gateway": "BINANCE",
        }
      ).encode()
    )
  )
  assert publisher.calls == []


async def test_worker_connected_missing_gateway_is_rejected():
  consumer, _repo, publisher = _make_consumer()
  await consumer.handle_subject_system(
    FakeMsg(
      json.dumps(
        {
          "action": "WORKER_CONNECTED",
          "account_id": "CRYPTO-BINANCE-7654321",
          "market": "CRYPTO",
        }
      ).encode()
    )
  )
  assert publisher.calls == []


async def test_non_crypto_market_does_not_publish():
  consumer, _repo, publisher = _make_consumer()
  await consumer.handle_subject_system(
    FakeMsg(
      _worker_connected_payload(
        account_id="FOREX-MT5-12345678", market="FOREX", gateway="MT5"
      )
    )
  )
  assert publisher.calls == []


async def test_missing_settings_skips_publish():
  consumer, _repo, publisher = _make_consumer(
    settings={CRYPTO_ALLOWED_SYMBOL_KEY: None, CRYPTO_MAX_LEVERAGE_KEY: None}
  )
  await consumer.handle_subject_system(FakeMsg(_worker_connected_payload()))
  assert publisher.calls == []


async def test_non_integer_leverage_skips_publish():
  consumer, _repo, publisher = _make_consumer(
    settings={CRYPTO_ALLOWED_SYMBOL_KEY: "BTC,ETH", CRYPTO_MAX_LEVERAGE_KEY: "ten"}
  )
  await consumer.handle_subject_system(FakeMsg(_worker_connected_payload()))
  assert publisher.calls == []


async def test_symbols_are_trimmed_and_filtered():
  consumer, _repo, publisher = _make_consumer(
    settings={CRYPTO_ALLOWED_SYMBOL_KEY: " BTC ,, ETH ", CRYPTO_MAX_LEVERAGE_KEY: "5"}
  )
  await consumer.handle_subject_system(FakeMsg(_worker_connected_payload()))
  assert publisher.calls[0]["symbols"] == ["BTC", "ETH"]
  assert publisher.calls[0]["default_leverage"] == 5


class FakeSubscription:
  def __init__(self):
    self.unsubscribed = False

  async def unsubscribe(self):
    self.unsubscribed = True


class FakeConnNC:
  def __init__(self):
    self.subscribed_to: str | None = None
    self._sub = FakeSubscription()

  async def subscribe(self, subject, cb):
    self.subscribed_to = subject
    return self._sub


class FakeConn:
  def __init__(self):
    self.nc = FakeConnNC()


async def test_start_subscribes_to_system_subject():
  consumer, _repo, _pub = _make_consumer()
  conn = FakeConn()
  consumer._conn = conn  # type: ignore[attr-defined]
  await consumer.start()
  assert conn.nc.subscribed_to == PublishTopicEnum.SYSTEM.value


async def test_stop_unsubscribes():
  consumer, _repo, _pub = _make_consumer()
  conn = FakeConn()
  consumer._conn = conn  # type: ignore[attr-defined]
  await consumer.start()
  await consumer.stop()
  assert conn.nc._sub.unsubscribed is True
