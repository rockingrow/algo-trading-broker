import json

from broker.constants import (
  CRYPTO_ALLOWED_SYMBOL_KEY,
  CRYPTO_MAX_LEVERAGE_KEY,
  STRATEGY_MAGIC_MAP_KEY,
)
from broker.schemas.account_schema import MarketTypeEnum
from broker.schemas.publisher_schema import PublishTopicEnum
from broker.services.nats_service import SystemEventConsumer


class FakeAccountRepo:
  def __init__(self, settings: dict | None = None):
    self.upserts: list[tuple[str, MarketTypeEnum, str]] = []
    self.settings_reads: list[tuple[str, MarketTypeEnum, str]] = []
    self._settings = settings if settings is not None else {}

  async def upsert_gateway(
    self, account_id: str, market: MarketTypeEnum, gateway: str
  ) -> None:
    self.upserts.append((account_id, market, gateway))

  async def get_settings(
    self, account_id: str, market: MarketTypeEnum, gateway: str
  ) -> dict:
    self.settings_reads.append((account_id, market, gateway))
    return dict(self._settings)

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
    # publish_system_signal is the standalone CRYPTO_LEVERAGE_INIT the admin
    # push uses; the handshake must never reach for it any more.
    self.calls: list[dict] = []
    self.acks: list[dict] = []
    self.errors: list[dict] = []
    # Records the action of every SYSTEM publish in call order, so tests can
    # assert the handshake answers with exactly one message.
    self.order: list[str] = []

  async def publish(self, signal) -> None:
    return None

  async def publish_flat(self, **kwargs) -> None:
    return None

  async def publish_admin_signal(self, **kwargs) -> None:
    return None

  async def publish_system_signal(self, **kwargs) -> None:
    self.calls.append(kwargs)
    self.order.append("CRYPTO_LEVERAGE_INIT")

  async def publish_system_ack(self, **kwargs) -> None:
    self.acks.append(kwargs)
    self.order.append("WORKER_CONNECTED_ACK")

  async def publish_system_error(self, **kwargs) -> None:
    self.errors.append(kwargs)
    self.order.append("WORKER_CONNECTED_ERROR")


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


# ── One reply per handshake ────────────────────────────────────────────────
#
# A NATS reply inbox only accepts one message, so the whole answer has to be a
# single ACK. These tests pin that invariant down.


async def test_handshake_answers_with_exactly_one_message():
  signals = FakeSignalRepo(envelopes=[_webhook_envelope("MT5_GOLD_M5_V1")])
  consumer, _repo, publisher = _make_consumer(
    settings=_magic_map_settings(), signals=signals
  )
  await consumer.handle_subject_system(
    FakeMsg(
      _worker_connected_payload(strategies=["MT5_GOLD_M5_V1"]),
      reply="_INBOX.crypto",
    )
  )

  # A crypto worker with strategies has all three blocks to deliver, and they
  # still travel in one message — anything after the first would be dropped by
  # the inbox.
  assert publisher.order == ["WORKER_CONNECTED_ACK"]
  ack = publisher.acks[0]
  assert ack["subject"] == "_INBOX.crypto"
  assert ack["strategy_magic_map"] == {"MT5_GOLD_M5_V1": 20260409}
  assert len(ack["retry_signals"]) == 1
  assert ack["crypto_leverage_init"].symbols == ["BTC", "ETH"]
  assert ack["crypto_leverage_init"].default_leverage == 10


async def test_handshake_never_uses_the_standalone_leverage_publish():
  # CRYPTO_LEVERAGE_INIT survives only as the admin push to already-connected
  # workers; the connect-time copy rides inside the ACK.
  consumer, _repo, publisher = _make_consumer()
  await consumer.handle_subject_system(
    FakeMsg(_worker_connected_payload(), reply="_INBOX.abc")
  )
  assert publisher.calls == []


async def test_worker_connected_carries_crypto_leverage_config():
  consumer, _repo, publisher = _make_consumer()
  await consumer.handle_subject_system(FakeMsg(_worker_connected_payload()))

  assert len(publisher.acks) == 1
  ack = publisher.acks[0]
  assert ack["account_id"] == "CRYPTO-BINANCE-7654321"
  assert ack["crypto_leverage_init"].symbols == ["BTC", "ETH"]
  assert ack["crypto_leverage_init"].default_leverage == 10


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
  assert publisher.order == []


async def test_worker_connected_ack_echo_is_ignored():
  # The broker sees its own broadcast ACK on the shared SYSTEM subject and must
  # not treat it as a handshake.
  consumer, _repo, publisher = _make_consumer()
  payload = json.dumps(
    {
      "action": "WORKER_CONNECTED_ACK",
      "account_id": "CRYPTO-BINANCE-7654321",
      "timestamp": "2026-06-30T00:00:00+00:00",
      "strategy_magic_map": {},
      "retry_signals": [],
    }
  ).encode()
  await consumer.handle_subject_system(FakeMsg(payload))
  assert publisher.order == []


async def test_malformed_json_is_swallowed():
  consumer, _repo, publisher = _make_consumer()
  await consumer.handle_subject_system(FakeMsg(b"{not-json"))
  assert publisher.order == []


async def test_invalid_schema_is_swallowed():
  consumer, _repo, publisher = _make_consumer()
  await consumer.handle_subject_system(
    FakeMsg(json.dumps({"action": "WORKER_CONNECTED"}).encode())
  )
  assert publisher.order == []


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
  assert publisher.acks == []


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
  assert publisher.acks == []


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
  assert publisher.acks == []


async def test_non_crypto_market_gets_ack_without_leverage_block():
  consumer, _repo, publisher = _make_consumer()
  await consumer.handle_subject_system(
    FakeMsg(
      _worker_connected_payload(
        account_id="FOREX-MT5-12345678", market="FOREX", gateway="MT5"
      )
    )
  )
  assert len(publisher.acks) == 1
  assert publisher.acks[0]["crypto_leverage_init"] is None


async def test_missing_settings_send_no_ack():
  # A crypto worker whose settings are missing must not be told it is
  # configured; the handshake is rejected instead.
  consumer, _repo, publisher = _make_consumer(
    settings={CRYPTO_ALLOWED_SYMBOL_KEY: None, CRYPTO_MAX_LEVERAGE_KEY: None}
  )
  await consumer.handle_subject_system(FakeMsg(_worker_connected_payload()))
  assert publisher.acks == []


async def test_non_integer_leverage_sends_no_ack():
  consumer, _repo, publisher = _make_consumer(
    settings={CRYPTO_ALLOWED_SYMBOL_KEY: "BTC,ETH", CRYPTO_MAX_LEVERAGE_KEY: "ten"}
  )
  await consumer.handle_subject_system(FakeMsg(_worker_connected_payload()))
  assert publisher.acks == []


async def test_zero_leverage_sends_no_ack():
  consumer, _repo, publisher = _make_consumer(
    settings={CRYPTO_ALLOWED_SYMBOL_KEY: "BTC,ETH", CRYPTO_MAX_LEVERAGE_KEY: "0"}
  )
  await consumer.handle_subject_system(FakeMsg(_worker_connected_payload()))
  assert publisher.acks == []


async def test_negative_leverage_sends_no_ack():
  consumer, _repo, publisher = _make_consumer(
    settings={CRYPTO_ALLOWED_SYMBOL_KEY: "BTC,ETH", CRYPTO_MAX_LEVERAGE_KEY: "-5"}
  )
  await consumer.handle_subject_system(FakeMsg(_worker_connected_payload()))
  assert publisher.acks == []


async def test_negative_leverage_with_reply_inbox_gets_error():
  consumer, _repo, publisher = _make_consumer(
    settings={CRYPTO_ALLOWED_SYMBOL_KEY: "BTC,ETH", CRYPTO_MAX_LEVERAGE_KEY: "-5"}
  )
  await consumer.handle_subject_system(
    FakeMsg(_worker_connected_payload(), reply="_INBOX.err")
  )
  # The error is the whole answer: no ACK may follow it on the same inbox.
  assert publisher.order == ["WORKER_CONNECTED_ERROR"]
  assert "positive" in publisher.errors[0]["reason"]


async def test_rejected_crypto_handshake_skips_the_signals_query():
  # Nothing to replay to a worker we are about to reject, so the DB is spared.
  signals = FakeSignalRepo()
  consumer, _repo, _pub = _make_consumer(
    settings={CRYPTO_ALLOWED_SYMBOL_KEY: None, CRYPTO_MAX_LEVERAGE_KEY: None},
    signals=signals,
  )
  await consumer.handle_subject_system(
    FakeMsg(_worker_connected_payload(strategies=["wt_cross_v1"]), reply="_INBOX.err")
  )
  assert signals.calls == []


async def test_symbols_are_trimmed_and_filtered():
  consumer, _repo, publisher = _make_consumer(
    settings={CRYPTO_ALLOWED_SYMBOL_KEY: " BTC ,, ETH ", CRYPTO_MAX_LEVERAGE_KEY: "5"}
  )
  await consumer.handle_subject_system(FakeMsg(_worker_connected_payload()))
  leverage = publisher.acks[0]["crypto_leverage_init"]
  assert leverage.symbols == ["BTC", "ETH"]
  assert leverage.default_leverage == 5


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
  assert publisher.acks[0]["crypto_leverage_init"] is None


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


async def test_account_repo_failure_does_not_block_the_ack():
  class ExplodingAccountRepo(FakeAccountRepo):
    async def upsert_gateway(self, account_id, market, gateway) -> None:
      raise RuntimeError("db down")

  consumer, _repo, publisher = _make_consumer(accounts=ExplodingAccountRepo())
  await consumer.handle_subject_system(
    FakeMsg(_worker_connected_payload(), reply="_INBOX.abc")
  )

  # Bookkeeping is best-effort; the worker still gets its configuration.
  assert len(publisher.acks) == 1
  assert publisher.acks[0]["crypto_leverage_init"].default_leverage == 10


# ── accounts.settings inside the ACK ───────────────────────────────────────
#
# What the owner set from the bot (/prevent & co.) has to reach the worker on
# connect: the ADMIN push that ran the command only reached a worker that was
# online at the time.


async def test_ack_carries_the_accounts_settings_blob():
  accounts = FakeAccountRepo(settings={"signal_blocked": True})
  consumer, _repo, publisher = _make_consumer(accounts=accounts)
  await consumer.handle_subject_system(
    FakeMsg(_worker_connected_payload(), reply="_INBOX.crypto")
  )

  # Looked up under the bare account_id, same scoping as the gateway upsert.
  assert accounts.settings_reads == [("7654321", MarketTypeEnum.CRYPTO, "BINANCE")]
  assert publisher.acks[0]["settings"].signal_blocked is True


async def test_ack_settings_default_for_an_account_that_never_ran_a_command():
  # {} in the row (or no row at all) → the block is still present, with the
  # schema defaults, so a worker can read it unconditionally.
  consumer, _repo, publisher = _make_consumer(accounts=FakeAccountRepo(settings={}))
  await consumer.handle_subject_system(FakeMsg(_worker_connected_payload()))
  assert publisher.acks[0]["settings"].signal_blocked is False


async def test_ack_settings_ignores_unknown_keys():
  accounts = FakeAccountRepo(
    settings={"signal_blocked": True, "written_by_a_newer_broker": 42}
  )
  consumer, _repo, publisher = _make_consumer(accounts=accounts)
  await consumer.handle_subject_system(FakeMsg(_worker_connected_payload()))

  settings = publisher.acks[0]["settings"]
  assert settings.signal_blocked is True
  assert settings.model_dump() == {"signal_blocked": True}


async def test_ack_settings_fall_back_to_defaults_when_the_blob_is_invalid():
  accounts = FakeAccountRepo(settings={"signal_blocked": "not-a-bool"})
  consumer, _repo, publisher = _make_consumer(accounts=accounts)
  await consumer.handle_subject_system(
    FakeMsg(_worker_connected_payload(), reply="_INBOX.crypto")
  )

  # A hand-edited row must not cost the worker the rest of its configuration.
  assert len(publisher.acks) == 1
  assert publisher.acks[0]["settings"].signal_blocked is False
  assert publisher.acks[0]["crypto_leverage_init"].default_leverage == 10


async def test_settings_lookup_failure_still_sends_the_rest_of_the_config():
  class ExplodingSettingsRepo(FakeAccountRepo):
    async def get_settings(self, account_id, market, gateway) -> dict:
      raise RuntimeError("db down")

  consumer, _repo, publisher = _make_consumer(accounts=ExplodingSettingsRepo())
  await consumer.handle_subject_system(
    FakeMsg(_worker_connected_payload(), reply="_INBOX.crypto")
  )

  assert len(publisher.acks) == 1
  assert publisher.acks[0]["settings"].signal_blocked is False
  assert publisher.acks[0]["crypto_leverage_init"].default_leverage == 10


async def test_rejected_crypto_handshake_skips_the_settings_query():
  # Same reason as the signals query: nothing to configure on a worker we are
  # about to reject.
  accounts = FakeAccountRepo(settings={"signal_blocked": True})
  consumer, _repo, _pub = _make_consumer(
    settings={CRYPTO_ALLOWED_SYMBOL_KEY: None, CRYPTO_MAX_LEVERAGE_KEY: None},
    accounts=accounts,
  )
  await consumer.handle_subject_system(
    FakeMsg(_worker_connected_payload(), reply="_INBOX.err")
  )
  assert accounts.settings_reads == []


async def test_settings_are_read_fresh_on_every_handshake():
  # Deliberately uncached: a command run between two connects must reach the
  # second one.
  accounts = FakeAccountRepo(settings={"signal_blocked": False})
  consumer, _repo, publisher = _make_consumer(accounts=accounts)

  await consumer.handle_subject_system(FakeMsg(_worker_connected_payload()))
  accounts._settings = {"signal_blocked": True}
  await consumer.handle_subject_system(FakeMsg(_worker_connected_payload()))

  assert len(accounts.settings_reads) == 2
  assert publisher.acks[0]["settings"].signal_blocked is False
  assert publisher.acks[1]["settings"].signal_blocked is True


# ── Request/reply (worker used nats.request, msg carries a reply inbox) ────────


async def test_no_reply_inbox_broadcasts_on_system_subject():
  consumer, _repo, publisher = _make_consumer()
  await consumer.handle_subject_system(FakeMsg(_worker_connected_payload()))
  # subject=None → NatsPublisher falls back to the shared SYSTEM subject.
  assert publisher.acks[0]["subject"] is None
  assert publisher.errors == []


async def test_reply_inbox_gets_the_ack_directly():
  consumer, _repo, publisher = _make_consumer()
  await consumer.handle_subject_system(
    FakeMsg(_worker_connected_payload(), reply="_INBOX.abc")
  )
  assert len(publisher.acks) == 1
  assert publisher.acks[0]["subject"] == "_INBOX.abc"
  assert publisher.acks[0]["crypto_leverage_init"].symbols == ["BTC", "ETH"]
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
  assert len(publisher.acks) == 1
  assert publisher.acks[0]["subject"] == "_INBOX.forex"
  assert publisher.acks[0]["account_id"] == "FOREX-MT5-12345678"


async def test_non_crypto_without_reply_inbox_is_broadcast():
  consumer, _repo, publisher = _make_consumer()
  await consumer.handle_subject_system(
    FakeMsg(
      _worker_connected_payload(
        account_id="FOREX-MT5-12345678", market="FOREX", gateway="MT5"
      )
    )
  )
  # The fire-and-forget worker still gets its config, filtered by account_id.
  assert len(publisher.acks) == 1
  assert publisher.acks[0]["subject"] is None
  assert publisher.errors == []


async def test_missing_settings_with_reply_inbox_gets_error():
  consumer, _repo, publisher = _make_consumer(
    settings={CRYPTO_ALLOWED_SYMBOL_KEY: None, CRYPTO_MAX_LEVERAGE_KEY: None}
  )
  await consumer.handle_subject_system(
    FakeMsg(_worker_connected_payload(), reply="_INBOX.err")
  )
  assert publisher.acks == []
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
  assert publisher.acks == []
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
  assert publisher.acks == []
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
  assert publisher.acks == []
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
  assert publisher.order == []


class ExplodingPublisher(FakePublisher):
  async def publish_system_ack(self, **kwargs) -> None:
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
  assert len(publisher.acks) == 2
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
  assert len(publisher.acks) == 3


async def test_missing_settings_are_also_cached():
  # A "not configured" read is cached too so a reconnect storm during an
  # ongoing misconfiguration doesn't hammer the DB either.
  consumer, repo, publisher = _make_consumer(
    settings={CRYPTO_ALLOWED_SYMBOL_KEY: None, CRYPTO_MAX_LEVERAGE_KEY: None}
  )
  await consumer.handle_subject_system(FakeMsg(_worker_connected_payload()))
  await consumer.handle_subject_system(FakeMsg(_worker_connected_payload()))

  assert len(repo.get_many_calls) == 1
  assert publisher.acks == []


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


# ── retry_signals replay inside the ACK ────────────────────────────────────


def _webhook_envelope(
  strategy: str, signal_id: str = "sig-1", signal_uxid: str = "0000111122223333"
) -> dict:
  return {
    "signal_id": signal_id,
    "payload": {
      "signal_uxid": signal_uxid,
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

  assert len(publisher.acks) == 1
  ack = publisher.acks[0]
  assert ack["account_id"] == "FOREX-MT5-1"
  assert ack["subject"] == "_INBOX.forex"
  assert len(ack["retry_signals"]) == 1
  # Payload mirrors SIGNAL exactly (symbol normalised, strategy carried).
  assert ack["retry_signals"][0].symbol == "XAUUSD"
  assert ack["retry_signals"][0].strategy == "wt_cross_v1"


async def test_retry_signal_replays_across_many_strategies():
  """A worker connecting with many strategy subjects (e.g. 10) gets every
  matching signal back in the one ACK — one DB lookup and one publish, not one
  round trip per strategy."""
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

  # All matching signals come back inside exactly one ACK.
  assert len(publisher.acks) == 1
  ack = publisher.acks[0]
  assert ack["subject"] == "_INBOX.many"
  assert len(ack["retry_signals"]) == 10
  assert {s.strategy for s in ack["retry_signals"]} == set(strategies)


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


async def test_retry_signals_empty_without_announced_strategies():
  signals = FakeSignalRepo()
  consumer, _repo, publisher = _make_consumer(signals=signals)
  # No `strategies` field → default_factory gives []; nothing to replay.
  await consumer.handle_subject_system(FakeMsg(_worker_connected_payload()))
  assert signals.calls == []
  assert publisher.acks[0]["retry_signals"] == []


async def test_retry_signals_empty_when_no_signal_repository():
  # Existing deployments that don't wire a SignalRepository must still work.
  consumer, _repo, publisher = _make_consumer()
  await consumer.handle_subject_system(
    FakeMsg(_worker_connected_payload(strategies=["wt_cross_v1"]))
  )
  assert publisher.acks[0]["retry_signals"] == []


async def test_signals_lookup_failure_still_sends_the_rest_of_the_config():
  class ExplodingSignalRepo(FakeSignalRepo):
    async def list_recent_by_strategies(self, strategies, since_seconds):
      raise RuntimeError("db down")

  consumer, _repo, publisher = _make_consumer(
    settings=_magic_map_settings(), signals=ExplodingSignalRepo()
  )
  await consumer.handle_subject_system(
    FakeMsg(
      _worker_connected_payload(strategies=["MT5_GOLD_M5_V1"]), reply="_INBOX.crypto"
    )
  )
  # A missed replay must not cost the worker its magic map and leverage config.
  assert len(publisher.acks) == 1
  ack = publisher.acks[0]
  assert ack["retry_signals"] == []
  assert ack["strategy_magic_map"] == {"MT5_GOLD_M5_V1": 20260409}
  assert ack["crypto_leverage_init"].default_leverage == 10


async def test_retry_signal_bad_envelope_is_skipped_but_others_replayed():
  signals = FakeSignalRepo(
    envelopes=[
      {"signal_id": "sig-bad", "payload": {"not": "a webhook"}},
      _webhook_envelope("wt_cross_v1"),
    ]
  )
  consumer, _repo, publisher = _make_consumer(signals=signals)
  await consumer.handle_subject_system(
    FakeMsg(_worker_connected_payload(strategies=["wt_cross_v1"]))
  )
  assert len(publisher.acks) == 1
  retry = publisher.acks[0]["retry_signals"]
  assert len(retry) == 1
  assert retry[0].signal_id == "sig-1"


async def test_replayed_signal_carries_both_ids():
  """The replay repeats the id the signal was published with — that is what a
  worker de-duplicates on — and the cycle id rides along from the payload."""
  signals = FakeSignalRepo(
    envelopes=[
      _webhook_envelope(
        "wt_cross_v1", signal_id="sig-7", signal_uxid="9f2c4b7e18a3d605"
      )
    ]
  )
  consumer, _repo, publisher = _make_consumer(signals=signals)
  await consumer.handle_subject_system(
    FakeMsg(_worker_connected_payload(strategies=["wt_cross_v1"]))
  )
  replayed = publisher.acks[0]["retry_signals"][0]
  assert replayed.signal_id == "sig-7"
  assert replayed.signal_uxid == "9f2c4b7e18a3d605"


# ── strategy_magic_map inside the ACK ──────────────────────────────────────

_MAGIC_MAP_JSON = (
  '{"MT5_GOLD_M5_V1": 20260409, "SIDEWAY_M15_V1": 20260617, '
  '"MT5_MULTI_M5_V1": 20260708}'
)


def _magic_map_settings(extra: dict[str, str | None] | None = None) -> dict:
  base = {
    CRYPTO_ALLOWED_SYMBOL_KEY: "BTC,ETH",
    CRYPTO_MAX_LEVERAGE_KEY: "10",
    STRATEGY_MAGIC_MAP_KEY: _MAGIC_MAP_JSON,
  }
  if extra:
    base.update(extra)
  return base


async def test_magic_map_filtered_to_announced_strategies():
  # A forex worker announces a subset of the mapped strategies; only those are
  # returned.
  consumer, _repo, publisher = _make_consumer(settings=_magic_map_settings())
  await consumer.handle_subject_system(
    FakeMsg(
      _worker_connected_payload(
        account_id="FOREX-MT5-1",
        market="FOREX",
        gateway="MT5",
        strategies=["MT5_GOLD_M5_V1", "MT5_MULTI_M5_V1", "NOT_MAPPED"],
      ),
      reply="_INBOX.forex",
    )
  )
  assert len(publisher.acks) == 1
  ack = publisher.acks[0]
  assert ack["account_id"] == "FOREX-MT5-1"
  assert ack["subject"] == "_INBOX.forex"
  # Only announced-and-mapped strategies survive; SIDEWAY_M15_V1 (mapped but not
  # announced) and NOT_MAPPED (announced but not mapped) are both excluded.
  assert ack["strategy_magic_map"] == {
    "MT5_GOLD_M5_V1": 20260409,
    "MT5_MULTI_M5_V1": 20260708,
  }


async def test_magic_map_sent_for_crypto_too():
  consumer, _repo, publisher = _make_consumer(settings=_magic_map_settings())
  await consumer.handle_subject_system(
    FakeMsg(
      _worker_connected_payload(strategies=["SIDEWAY_M15_V1"]),
      reply="_INBOX.crypto",
    )
  )
  ack = publisher.acks[0]
  assert ack["strategy_magic_map"] == {"SIDEWAY_M15_V1": 20260617}
  # Crypto still also gets its leverage config, in the same message.
  assert ack["crypto_leverage_init"].default_leverage == 10


async def test_magic_map_empty_without_announced_strategies():
  # No strategies → the mandatory block is still sent, empty, and the setting is
  # not even read (nothing could match).
  repo = FakeSettingRepo(_magic_map_settings())
  publisher = FakePublisher()
  consumer = SystemEventConsumer(
    setting_repository=repo,
    account_repository=FakeAccountRepo(),
    publisher=publisher,
  )
  await consumer.handle_subject_system(
    FakeMsg(_worker_connected_payload(strategies=[]))
  )
  assert publisher.acks[0]["strategy_magic_map"] == {}
  assert STRATEGY_MAGIC_MAP_KEY not in repo.get_calls


async def test_magic_map_empty_when_setting_missing():
  consumer, _repo, publisher = _make_consumer(
    settings=_magic_map_settings({STRATEGY_MAGIC_MAP_KEY: None})
  )
  await consumer.handle_subject_system(
    FakeMsg(_worker_connected_payload(strategies=["MT5_GOLD_M5_V1"]))
  )
  assert publisher.acks[0]["strategy_magic_map"] == {}


async def test_magic_map_empty_when_setting_invalid_json():
  consumer, _repo, publisher = _make_consumer(
    settings=_magic_map_settings({STRATEGY_MAGIC_MAP_KEY: "{not valid"})
  )
  await consumer.handle_subject_system(
    FakeMsg(_worker_connected_payload(strategies=["MT5_GOLD_M5_V1"]))
  )
  assert publisher.acks[0]["strategy_magic_map"] == {}


async def test_magic_map_broadcast_without_reply_inbox():
  consumer, _repo, publisher = _make_consumer(settings=_magic_map_settings())
  await consumer.handle_subject_system(
    FakeMsg(_worker_connected_payload(strategies=["MT5_GOLD_M5_V1"]))
  )
  # No reply inbox → subject None so NatsPublisher broadcasts on SYSTEM.
  assert publisher.acks[0]["subject"] is None


async def test_magic_map_read_is_cached_within_ttl():
  # Two forex handshakes (no signal repo, so no retry-timeout read) within the
  # TTL read the strategy_magic_map setting only once.
  consumer, repo, publisher = _make_consumer(settings=_magic_map_settings())
  payload = _worker_connected_payload(
    account_id="FOREX-MT5-1",
    market="FOREX",
    gateway="MT5",
    strategies=["MT5_GOLD_M5_V1"],
  )
  await consumer.handle_subject_system(FakeMsg(payload))
  await consumer.handle_subject_system(FakeMsg(payload))
  assert len(publisher.acks) == 2
  assert repo.get_calls == [STRATEGY_MAGIC_MAP_KEY]
