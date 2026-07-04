import json

from broker.constants import CRYPTO_ALLOWED_SYMBOL_KEY, CRYPTO_MAX_LEVERAGE_KEY
from broker.schemas.publisher_schema import PublishTopicEnum, SystemActionEnum
from broker.services.nats_service import SystemEventConsumer


class FakeSettingRepo:
  def __init__(self, values: dict[str, str | None] | None = None):
    self._values = values or {}
    self.get_calls: list[str] = []
    self.get_many_calls: list[list[str]] = []

  async def get(self, key: str) -> str | None:
    self.get_calls.append(key)
    return self._values.get(key)

  async def get_many(self, keys: list[str]) -> dict[str, str]:
    self.get_many_calls.append(list(keys))
    return {k: v for k, v in self._values.items() if k in keys and v is not None}

  async def set(self, key: str, value: str) -> bool:
    self._values[key] = value
    return True


class FakePublisher:
  def __init__(self):
    self.calls: list[dict] = []
    self.acks: list[dict] = []
    self.errors: list[dict] = []

  async def publish(self, signal) -> None:
    return None

  async def publish_flat(self, **kwargs) -> None:
    return None

  async def publish_admin_signal(self, **kwargs) -> None:
    return None

  async def publish_system_signal(self, **kwargs) -> None:
    self.calls.append(kwargs)

  async def publish_system_ack(self, **kwargs) -> None:
    self.acks.append(kwargs)

  async def publish_system_error(self, **kwargs) -> None:
    self.errors.append(kwargs)


class FakeMsg:
  def __init__(self, data: bytes, reply: str = ""):
    self.data = data
    self.reply = reply


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


async def test_zero_leverage_skips_publish():
  consumer, _repo, publisher = _make_consumer(
    settings={CRYPTO_ALLOWED_SYMBOL_KEY: "BTC,ETH", CRYPTO_MAX_LEVERAGE_KEY: "0"}
  )
  await consumer.handle_subject_system(FakeMsg(_worker_connected_payload()))
  assert publisher.calls == []


async def test_negative_leverage_skips_publish():
  consumer, _repo, publisher = _make_consumer(
    settings={CRYPTO_ALLOWED_SYMBOL_KEY: "BTC,ETH", CRYPTO_MAX_LEVERAGE_KEY: "-5"}
  )
  await consumer.handle_subject_system(FakeMsg(_worker_connected_payload()))
  assert publisher.calls == []


async def test_negative_leverage_with_reply_inbox_gets_error():
  consumer, _repo, publisher = _make_consumer(
    settings={CRYPTO_ALLOWED_SYMBOL_KEY: "BTC,ETH", CRYPTO_MAX_LEVERAGE_KEY: "-5"}
  )
  await consumer.handle_subject_system(
    FakeMsg(_worker_connected_payload(), reply="_INBOX.err")
  )
  assert publisher.calls == []
  assert len(publisher.errors) == 1
  assert "positive" in publisher.errors[0]["reason"]


async def test_symbols_are_trimmed_and_filtered():
  consumer, _repo, publisher = _make_consumer(
    settings={CRYPTO_ALLOWED_SYMBOL_KEY: " BTC ,, ETH ", CRYPTO_MAX_LEVERAGE_KEY: "5"}
  )
  await consumer.handle_subject_system(FakeMsg(_worker_connected_payload()))
  assert publisher.calls[0]["symbols"] == ["BTC", "ETH"]
  assert publisher.calls[0]["default_leverage"] == 5


# ── Request/reply (worker used nats.request, msg carries a reply inbox) ────────


async def test_no_reply_inbox_broadcasts_on_system_subject():
  consumer, _repo, publisher = _make_consumer()
  await consumer.handle_subject_system(FakeMsg(_worker_connected_payload()))
  # subject=None → NatsPublisher falls back to the shared SYSTEM subject.
  assert publisher.calls[0]["subject"] is None
  assert publisher.acks == []
  assert publisher.errors == []


async def test_reply_inbox_gets_crypto_leverage_init_directly():
  consumer, _repo, publisher = _make_consumer()
  await consumer.handle_subject_system(
    FakeMsg(_worker_connected_payload(), reply="_INBOX.abc")
  )
  assert len(publisher.calls) == 1
  assert publisher.calls[0]["subject"] == "_INBOX.abc"
  assert publisher.calls[0]["symbols"] == ["BTC", "ETH"]
  # A direct reply must not also broadcast on the shared subject.
  assert publisher.acks == []
  assert publisher.errors == []


async def test_non_crypto_with_reply_inbox_gets_ack():
  consumer, _repo, publisher = _make_consumer()
  await consumer.handle_subject_system(
    FakeMsg(
      _worker_connected_payload(
        account_id="FOREX-MT5-12345678", market="FOREX", gateway="MT5"
      ),
      reply="_INBOX.forex",
    )
  )
  assert publisher.calls == []
  assert len(publisher.acks) == 1
  assert publisher.acks[0]["subject"] == "_INBOX.forex"
  assert publisher.acks[0]["account_id"] == "FOREX-MT5-12345678"


async def test_non_crypto_without_reply_inbox_stays_silent():
  consumer, _repo, publisher = _make_consumer()
  await consumer.handle_subject_system(
    FakeMsg(
      _worker_connected_payload(
        account_id="FOREX-MT5-12345678", market="FOREX", gateway="MT5"
      )
    )
  )
  assert publisher.acks == []
  assert publisher.errors == []


async def test_missing_settings_with_reply_inbox_gets_error():
  consumer, _repo, publisher = _make_consumer(
    settings={CRYPTO_ALLOWED_SYMBOL_KEY: None, CRYPTO_MAX_LEVERAGE_KEY: None}
  )
  await consumer.handle_subject_system(
    FakeMsg(_worker_connected_payload(), reply="_INBOX.err")
  )
  assert publisher.calls == []
  assert len(publisher.errors) == 1
  assert publisher.errors[0]["subject"] == "_INBOX.err"
  assert publisher.errors[0]["account_id"] == "CRYPTO-BINANCE-7654321"
  assert "settings" in publisher.errors[0]["reason"]


async def test_non_integer_leverage_with_reply_inbox_gets_error():
  consumer, _repo, publisher = _make_consumer(
    settings={CRYPTO_ALLOWED_SYMBOL_KEY: "BTC,ETH", CRYPTO_MAX_LEVERAGE_KEY: "ten"}
  )
  await consumer.handle_subject_system(
    FakeMsg(_worker_connected_payload(), reply="_INBOX.err")
  )
  assert publisher.calls == []
  assert len(publisher.errors) == 1
  assert publisher.errors[0]["subject"] == "_INBOX.err"


async def test_invalid_schema_with_reply_inbox_gets_error():
  consumer, _repo, publisher = _make_consumer()
  await consumer.handle_subject_system(
    FakeMsg(
      json.dumps(
        {"action": "WORKER_CONNECTED", "account_id": "CRYPTO-BINANCE-1"}
      ).encode(),
      reply="_INBOX.err",
    )
  )
  assert publisher.calls == []
  assert len(publisher.errors) == 1
  assert publisher.errors[0]["account_id"] == "CRYPTO-BINANCE-1"


async def test_malformed_json_with_reply_inbox_gets_error():
  consumer, _repo, publisher = _make_consumer()
  await consumer.handle_subject_system(FakeMsg(b"{not-json", reply="_INBOX.err"))
  assert len(publisher.errors) == 1
  assert publisher.errors[0]["account_id"] is None


async def test_non_object_json_is_handled():
  # Valid JSON that is not an object must not crash the callback.
  consumer, _repo, publisher = _make_consumer()
  await consumer.handle_subject_system(FakeMsg(b"[1, 2, 3]"))
  await consumer.handle_subject_system(FakeMsg(b"123", reply="_INBOX.err"))
  assert publisher.calls == []
  # Only the request-mode message (with a reply inbox) gets an error reply.
  assert len(publisher.errors) == 1
  assert publisher.errors[0]["account_id"] is None


async def test_crypto_leverage_init_echo_is_ignored_even_with_reply():
  # The broker must never react to its own outgoing actions, reply inbox or not.
  consumer, _repo, publisher = _make_consumer()
  payload = json.dumps(
    {"action": "CRYPTO_LEVERAGE_INIT", "account_id": "CRYPTO-BINANCE-7654321"}
  ).encode()
  await consumer.handle_subject_system(FakeMsg(payload, reply="_INBOX.x"))
  assert publisher.calls == []
  assert publisher.acks == []
  assert publisher.errors == []


class ExplodingPublisher(FakePublisher):
  async def publish_system_signal(self, **kwargs) -> None:
    raise RuntimeError("nats down")


async def test_publish_failure_is_swallowed():
  consumer, _repo, _pub = _make_consumer()
  consumer._publisher = ExplodingPublisher()  # type: ignore[attr-defined]
  # A NATS failure while replying must not propagate out of the callback.
  await consumer.handle_subject_system(
    FakeMsg(_worker_connected_payload(), reply="_INBOX.x")
  )


# ── Crypto settings cache (absorbs reconnect-storm bursts) ─────────────────


async def test_second_handshake_within_ttl_reuses_cached_settings():
  consumer, repo, publisher = _make_consumer()
  await consumer.handle_subject_system(FakeMsg(_worker_connected_payload()))
  await consumer.handle_subject_system(FakeMsg(_worker_connected_payload()))

  # Both handshakes got a reply, but the DB was only queried once.
  assert len(publisher.calls) == 2
  assert len(repo.get_many_calls) == 1
  # get() (the non-atomic, per-key path) must not be used at all.
  assert repo.get_calls == []


async def test_cache_miss_fetches_both_settings_in_one_query():
  consumer, repo, _publisher = _make_consumer()
  await consumer.handle_subject_system(FakeMsg(_worker_connected_payload()))

  assert len(repo.get_many_calls) == 1
  assert set(repo.get_many_calls[0]) == {
    CRYPTO_ALLOWED_SYMBOL_KEY,
    CRYPTO_MAX_LEVERAGE_KEY,
  }


async def test_cache_expires_after_ttl(monkeypatch):
  consumer, repo, publisher = _make_consumer()
  clock = {"now": 1_000.0}
  monkeypatch.setattr(
    "broker.services.nats_service.time.monotonic", lambda: clock["now"]
  )

  await consumer.handle_subject_system(FakeMsg(_worker_connected_payload()))
  assert len(repo.get_many_calls) == 1

  # Still within the TTL window: no re-fetch.
  clock["now"] += 1.0
  await consumer.handle_subject_system(FakeMsg(_worker_connected_payload()))
  assert len(repo.get_many_calls) == 1

  # Past the TTL: the next handshake re-reads the settings.
  clock["now"] += 30.0
  await consumer.handle_subject_system(FakeMsg(_worker_connected_payload()))
  assert len(repo.get_many_calls) == 2
  assert len(publisher.calls) == 3


async def test_missing_settings_are_also_cached():
  # A "not configured" read is cached too so a reconnect storm during an
  # ongoing misconfiguration doesn't hammer the DB either.
  consumer, repo, publisher = _make_consumer(
    settings={CRYPTO_ALLOWED_SYMBOL_KEY: None, CRYPTO_MAX_LEVERAGE_KEY: None}
  )
  await consumer.handle_subject_system(FakeMsg(_worker_connected_payload()))
  await consumer.handle_subject_system(FakeMsg(_worker_connected_payload()))

  assert len(repo.get_many_calls) == 1
  assert publisher.calls == []


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
