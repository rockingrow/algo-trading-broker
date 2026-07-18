import json

from broker.constants import CRYPTO_ALLOWED_SYMBOL_KEY, CRYPTO_MAX_LEVERAGE_KEY
from broker.schemas.account_schema import MarketTypeEnum
from broker.schemas.publisher_schema import PublishTopicEnum, SystemActionEnum
from broker.services.nats_service import SystemEventConsumer


class FakeAccountRepo:
  def __init__(self):
    self.upserts: list[tuple[str, MarketTypeEnum, str]] = []

  async def upsert_gateway(
    self, account_id: str, market: MarketTypeEnum, gateway: str
  ) -> None:
    self.upserts.append((account_id, market, gateway))

  async def get_all(self):
    return []

  async def get_by_market(self, market):
    return []


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
    self.retries: list[dict] = []
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

  async def publish_system_retry_signal(self, **kwargs) -> None:
    self.retries.append(kwargs)

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
  strategies: list[str] | None = None,
) -> bytes:
  body: dict = {
    "action": "WORKER_CONNECTED",
    "account_id": account_id,
    "timestamp": "2026-06-30T00:00:00+00:00",
    "market": market,
    "gateway": gateway,
  }
  if strategies is not None:
    body["strategies"] = strategies
  return json.dumps(body).encode()


class FakeSignalRepo:
  def __init__(self, envelopes: list[dict] | None = None):
    self._envelopes = list(envelopes or [])
    self.calls: list[tuple[list[str], int]] = []

  async def log_signal(self, payload):
    return "sig-id"

  async def mark_published(self, signal_id: str) -> bool:
    return True

  async def list_recent_by_strategies(
    self, strategies: list[str], since_seconds: int
  ) -> list[dict]:
    self.calls.append((list(strategies), since_seconds))
    return list(self._envelopes)


def _make_consumer(
  settings: dict[str, str | None] | None = None,
  accounts: FakeAccountRepo | None = None,
  signals: FakeSignalRepo | None = None,
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
  consumer = SystemEventConsumer(
    setting_repository=repo,
    account_repository=accounts or FakeAccountRepo(),
    publisher=publisher,
    signal_repository=signals,
  )
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


# ── Recording the announced gateway on the accounts row ───────────────────────


async def test_worker_connected_records_gateway_under_bare_account_id():
  accounts = FakeAccountRepo()
  consumer, _repo, _pub = _make_consumer(accounts=accounts)
  await consumer.handle_subject_system(FakeMsg(_worker_connected_payload()))

  # The worker announces its full worker id; the accounts row is keyed by the
  # bare account_id, so the <market>-<gateway>- prefix must be stripped.
  assert accounts.upserts == [("7654321", MarketTypeEnum.CRYPTO, "BINANCE")]


async def test_non_crypto_worker_also_records_gateway():
  accounts = FakeAccountRepo()
  consumer, _repo, publisher = _make_consumer(accounts=accounts)
  await consumer.handle_subject_system(
    FakeMsg(
      _worker_connected_payload(
        account_id="FOREX-MT5-12345678", market="FOREX", gateway="MT5"
      )
    )
  )

  assert accounts.upserts == [("12345678", MarketTypeEnum.FOREX, "MT5")]
  assert publisher.calls == []


async def test_bare_account_id_is_recorded_unchanged():
  # A worker that announces without the prefix has nothing to strip.
  accounts = FakeAccountRepo()
  consumer, _repo, _pub = _make_consumer(accounts=accounts)
  await consumer.handle_subject_system(
    FakeMsg(_worker_connected_payload(account_id="7654321"))
  )

  assert accounts.upserts == [("7654321", MarketTypeEnum.CRYPTO, "BINANCE")]


async def test_invalid_payloads_record_nothing():
  accounts = FakeAccountRepo()
  consumer, _repo, _pub = _make_consumer(accounts=accounts)
  await consumer.handle_subject_system(FakeMsg(b"{not-json"))
  await consumer.handle_subject_system(
    FakeMsg(json.dumps({"action": "WORKER_CONNECTED"}).encode())
  )
  await consumer.handle_subject_system(
    FakeMsg(
      json.dumps(
        {"action": "CRYPTO_LEVERAGE_INIT", "account_id": "CRYPTO-BINANCE-7654321"}
      ).encode()
    )
  )

  assert accounts.upserts == []


async def test_account_repo_failure_does_not_block_leverage_init():
  class ExplodingAccountRepo(FakeAccountRepo):
    async def upsert_gateway(self, account_id, market, gateway) -> None:
      raise RuntimeError("db down")

  consumer, _repo, publisher = _make_consumer(accounts=ExplodingAccountRepo())
  await consumer.handle_subject_system(
    FakeMsg(_worker_connected_payload(), reply="_INBOX.abc")
  )

  # Bookkeeping is best-effort; the worker still gets its configuration.
  assert len(publisher.calls) == 1
  assert publisher.calls[0]["action"] == SystemActionEnum.CRYPTO_LEVERAGE_INIT


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


# ── RETRY_SIGNALS replay on WORKER_CONNECTED ───────────────────────────────


def _webhook_envelope(strategy: str, signal_id: str = "sig-1") -> dict:
  return {
    "signal_id": signal_id,
    "payload": {
      "strategy": strategy,
      "symbol": "OANDA:XAUUSD",
      "timeframe": "60",
      "timestamp": "2026-06-30T00:00:00+00:00",
      "position": {
        "action": "LONG",
        "price": 100.0,
        "quantity": 1.0,
      },
      "token": "secret",
    },
  }


async def test_retry_signal_queries_and_replays_matching_signals():
  signals = FakeSignalRepo(envelopes=[_webhook_envelope("wt_cross_v1")])
  consumer, _repo, publisher = _make_consumer(signals=signals)
  await consumer.handle_subject_system(
    FakeMsg(
      _worker_connected_payload(
        account_id="FOREX-MT5-1",
        market="FOREX",
        gateway="MT5",
        strategies=["wt_cross_v1"],
      ),
      reply="_INBOX.forex",
    )
  )

  # The strategies list and default (60s) window drive the lookup.
  assert signals.calls == [(["wt_cross_v1"], 60)]

  assert len(publisher.retries) == 1
  retry = publisher.retries[0]
  assert retry["account_id"] == "FOREX-MT5-1"
  assert retry["subject"] == "_INBOX.forex"
  assert len(retry["signals"]) == 1
  # Payload mirrors SIGNAL exactly (symbol normalised, strategy carried).
  assert retry["signals"][0].symbol == "XAUUSD"
  assert retry["signals"][0].strategy == "wt_cross_v1"


async def test_retry_signal_replays_across_many_strategies():
  """A worker connecting with many strategy subjects (e.g. 10) gets every
  matching signal back in a single RETRY_SIGNALS batch — one DB lookup and
  one publish, not one round trip per strategy."""
  strategies = [f"strategy_{i}" for i in range(10)]
  envelopes = [
    _webhook_envelope(strategy, signal_id=f"sig-{i}")
    for i, strategy in enumerate(strategies)
  ]
  signals = FakeSignalRepo(envelopes=envelopes)
  consumer, _repo, publisher = _make_consumer(
    settings={
      CRYPTO_ALLOWED_SYMBOL_KEY: "BTC,ETH",
      CRYPTO_MAX_LEVERAGE_KEY: "10",
    },
    signals=signals,
  )
  await consumer.handle_subject_system(
    FakeMsg(
      _worker_connected_payload(
        account_id="CRYPTO-BINANCE-7654321",
        strategies=strategies,
      ),
      reply="_INBOX.many",
    )
  )

  # The full strategy list is passed to a single lookup call, not looped.
  assert signals.calls == [(strategies, 60)]

  # All matching signals come back as exactly one RETRY_SIGNALS batch.
  assert len(publisher.retries) == 1
  retry = publisher.retries[0]
  assert retry["subject"] == "_INBOX.many"
  assert len(retry["signals"]) == 10
  assert {s.strategy for s in retry["signals"]} == set(strategies)


async def test_retry_signal_uses_configured_timeout():
  signals = FakeSignalRepo()
  consumer, _repo, _pub = _make_consumer(
    settings={
      CRYPTO_ALLOWED_SYMBOL_KEY: "BTC,ETH",
      CRYPTO_MAX_LEVERAGE_KEY: "10",
      "max_retry_timeout": "120",
    },
    signals=signals,
  )
  await consumer.handle_subject_system(
    FakeMsg(_worker_connected_payload(strategies=["wt_cross_v1"]))
  )
  assert signals.calls == [(["wt_cross_v1"], 120)]


async def test_retry_signal_defaults_when_timeout_setting_invalid():
  signals = FakeSignalRepo()
  consumer, _repo, _pub = _make_consumer(
    settings={
      CRYPTO_ALLOWED_SYMBOL_KEY: "BTC,ETH",
      CRYPTO_MAX_LEVERAGE_KEY: "10",
      "max_retry_timeout": "bad",
    },
    signals=signals,
  )
  await consumer.handle_subject_system(
    FakeMsg(_worker_connected_payload(strategies=["wt_cross_v1"]))
  )
  assert signals.calls == [(["wt_cross_v1"], 60)]


async def test_retry_signal_skipped_without_strategies():
  signals = FakeSignalRepo()
  consumer, _repo, publisher = _make_consumer(signals=signals)
  # No `strategies` field → default_factory gives []; nothing to replay.
  await consumer.handle_subject_system(FakeMsg(_worker_connected_payload()))
  assert signals.calls == []
  assert publisher.retries == []


async def test_retry_signal_skipped_when_no_signal_repository():
  # Existing deployments that don't wire a SignalRepository must still work.
  consumer, _repo, publisher = _make_consumer()
  await consumer.handle_subject_system(
    FakeMsg(_worker_connected_payload(strategies=["wt_cross_v1"]))
  )
  assert publisher.retries == []


async def test_retry_signal_sent_alongside_crypto_leverage_init():
  # A crypto worker gets both the RETRY_SIGNALS replay AND CRYPTO_LEVERAGE_INIT.
  signals = FakeSignalRepo(envelopes=[_webhook_envelope("wt_cross_v1")])
  consumer, _repo, publisher = _make_consumer(signals=signals)
  await consumer.handle_subject_system(
    FakeMsg(
      _worker_connected_payload(strategies=["wt_cross_v1"]), reply="_INBOX.abc"
    )
  )
  assert len(publisher.retries) == 1
  assert len(publisher.calls) == 1
  assert publisher.calls[0]["action"] == SystemActionEnum.CRYPTO_LEVERAGE_INIT


async def test_retry_signal_bad_envelope_is_skipped_but_others_replayed():
  signals = FakeSignalRepo(
    envelopes=[
      {"signal_id": "sig-bad", "payload": {"not": "a webhook"}},
      _webhook_envelope("wt_cross_v1", signal_id="sig-good"),
    ]
  )
  consumer, _repo, publisher = _make_consumer(signals=signals)
  await consumer.handle_subject_system(
    FakeMsg(_worker_connected_payload(strategies=["wt_cross_v1"]))
  )
  assert len(publisher.retries) == 1
  retry = publisher.retries[0]
  assert len(retry["signals"]) == 1
  assert retry["signals"][0].signal_id == "sig-good"
