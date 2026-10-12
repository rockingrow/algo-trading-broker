"""``make status``: one honest line per link, and an exit code to match.

Neither the engine nor a worker can be pinged, so both are read off the NATS
server's connection list. These pin how a connection is told apart — the
broker's own must never be counted as a worker — and the answers that are easy
to print wrong: a known account with no worker connected, a stream nobody
pulls from, and a monitor that could not be read, which must never be reported
as "nobody is connected".
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from nats.js.errors import NotFoundError

from broker import status
from broker.status import ConnectedWorker, KnownAccount, LinkReport, NatsClients

BROKER_CONNECTION = {
  "cid": 5,
  "ip": "172.18.0.4",
  "lang": "python3",
  "subscriptions_list": ["TRADE", "SYSTEM", "_INBOX.abc.*"],
}

ENGINE_CONNECTION = {
  "cid": 9,
  "name": "qte-broker-sink",
  "ip": "172.18.0.3",
  "lang": "python3",
  "subscriptions_list": ["_INBOX.def.*"],
}

WORKER_CONNECTION = {
  "cid": 12,
  "name": "mt5-worker",
  "ip": "172.18.0.3",
  "lang": "python3",
  "subscriptions_list": [
    "ADMIN",
    "ADMIN.FOREX.MT5.123456",
    "SYSTEM",
    "MT5_GOLD_M5_V1",
    "wt_cross_v1",
    "_INBOX.ghi.*",
  ],
}

GOLD_ACCOUNT = KnownAccount(
  market="FOREX",
  gateway="MT5",
  account_id="123456",
  account_name="Gold desk",
  last_activity_at=datetime(2026, 10, 12, 9, 30, tzinfo=timezone.utc),
)

CRYPTO_ACCOUNT = KnownAccount(
  market="CRYPTO",
  gateway="BINANCE",
  account_id="7654321",
  account_name=None,
  last_activity_at=None,
)


def _worker(account_id="123456", market="FOREX", gateway="MT5") -> ConnectedWorker:
  return ConnectedWorker(
    market=market,
    gateway=gateway,
    account_id=account_id,
    address="172.18.0.3",
    strategies=("MT5_GOLD_M5_V1",),
  )


# ── Telling connections apart ──────────────────────────────────────────────


def test_connections_are_sorted_into_engine_workers_and_the_rest():
  stranger = {"name": "nats-cli", "ip": "10.0.0.7", "subscriptions_list": ["ADMIN"]}

  clients = status.classify_connections(
    [BROKER_CONNECTION, ENGINE_CONNECTION, WORKER_CONNECTION, stranger]
  )

  assert clients.engines == [ENGINE_CONNECTION]
  assert [worker.worker_id for worker in clients.workers] == ["FOREX-MT5-123456"]
  assert clients.unidentified == [stranger]


def test_the_brokers_own_connection_is_never_a_worker_or_a_stranger():
  clients = status.classify_connections([BROKER_CONNECTION])

  assert clients == NatsClients()


def test_this_probes_own_connection_is_left_out():
  probe = {"name": status.STATUS_CLIENT_NAME, "subscriptions_list": ["_INBOX.x.*"]}

  assert status.classify_connections([probe]) == NatsClients()


def test_a_workers_strategies_exclude_the_plumbing_subjects():
  (worker,) = status.classify_connections([WORKER_CONNECTION]).workers

  assert worker.strategies == ("MT5_GOLD_M5_V1", "wt_cross_v1")


def test_a_strategy_named_like_a_reserved_subject_is_still_a_strategy():
  connection = {
    "subscriptions_list": ["ADMIN.CRYPTO.BINANCE.7654321", "ADMIN_HEDGE_V1"]
  }

  (worker,) = status.classify_connections([connection]).workers

  assert worker.strategies == ("ADMIN_HEDGE_V1",)


def test_a_client_with_no_subscription_list_is_listed_not_dropped():
  bare = {"name": "unknown-tool", "ip": "10.0.0.9"}

  assert status.classify_connections([bare]).unidentified == [bare]


# ── Engine ─────────────────────────────────────────────────────────────────


def test_a_connected_engine_is_reported_with_its_address():
  report = status.engine_report(NatsClients(engines=[ENGINE_CONNECTION]))

  assert report.verdict == "ok"
  assert "172.18.0.3" in report.detail


def test_a_missing_engine_is_a_warning_because_tradingview_alone_is_valid():
  report = status.engine_report(NatsClients())

  assert report.verdict == "warn"
  assert status.ENGINE_CLIENT_NAME in report.detail


# ── Workers ────────────────────────────────────────────────────────────────


def test_every_known_account_connected_is_a_pass():
  report = status.workers_report(NatsClients(workers=[_worker()]), [GOLD_ACCOUNT])

  assert report.verdict == "ok"
  assert report.detail == "1 connected, 0 known account(s) offline"
  assert report.listing[0].startswith("online   FOREX-MT5-123456")


def test_a_known_account_with_no_worker_is_listed_offline():
  report = status.workers_report(
    NatsClients(workers=[_worker()]), [GOLD_ACCOUNT, CRYPTO_ACCOUNT]
  )

  assert report.verdict == "warn"
  assert report.listing[-1] == "offline  CRYPTO-BINANCE-7654321  last activity never"


def test_an_offline_account_shows_its_name_and_last_activity():
  report = status.workers_report(NatsClients(), [GOLD_ACCOUNT])

  assert report.verdict == "warn"
  assert "reaches nobody" in report.detail
  assert report.listing == (
    "offline  FOREX-MT5-123456  (Gold desk)  last activity 2026-10-12 09:30 UTC",
  )


def test_an_account_without_a_gateway_matches_on_market_and_id():
  legacy = KnownAccount("FOREX", None, "123456", None, None)

  report = status.workers_report(NatsClients(workers=[_worker()]), [legacy])

  assert report.verdict == "ok"


def test_the_same_id_on_another_market_is_a_different_account():
  namesake = KnownAccount("CRYPTO", "BINANCE", "123456", None, None)

  report = status.workers_report(NatsClients(workers=[_worker()]), [namesake])

  assert report.verdict == "warn"
  assert "1 known account(s) offline" in report.detail


def test_unreadable_accounts_are_not_reported_as_an_empty_table():
  report = status.workers_report(NatsClients(workers=[_worker()]), None)

  assert report.verdict == "ok"
  assert "accounts table not read" in report.detail and "offline" not in report.detail


async def test_an_unreadable_monitor_never_reads_as_nobody_connected(monkeypatch):
  class RefusingClient:
    def __init__(self, *args, **kwargs):
      pass

    async def __aenter__(self):
      return self

    async def __aexit__(self, *exc_info):
      return False

    async def get(self, *args, **kwargs):
      raise ConnectionRefusedError("monitor port closed")

  monkeypatch.setattr(status.httpx, "AsyncClient", RefusingClient)

  engine, workers = await status.probe_clients([GOLD_ACCOUNT])

  assert engine.verdict == workers.verdict == "warn"
  assert "unverified" in engine.detail and "unverified" in workers.detail
  assert workers.listing == ()


# ── JetStream ──────────────────────────────────────────────────────────────


class FakeJetStream:
  def __init__(self, *, waiting=1, stream_missing=False, consumer_missing=False):
    self.waiting = waiting
    self.stream_missing = stream_missing
    self.consumer_missing = consumer_missing

  def jetstream(self):
    return self

  async def stream_info(self, name):
    if self.stream_missing:
      raise NotFoundError
    return SimpleNamespace(state=SimpleNamespace(messages=3))

  async def consumer_info(self, stream, consumer):
    if self.consumer_missing:
      raise NotFoundError
    return SimpleNamespace(num_waiting=self.waiting)


@pytest.fixture
def fast_sampling(monkeypatch):
  monkeypatch.setattr(status, "PULL_SAMPLE_GAP_SECONDS", 0)


async def test_a_pulled_stream_means_the_signal_worker_is_running():
  report = await status.probe_signal_stream(FakeJetStream())

  assert report.verdict == "ok"
  assert "3 undelivered" in report.detail and "is pulling" in report.detail


async def test_a_stream_nobody_pulls_from_fails_the_run(fast_sampling):
  report = await status.probe_signal_stream(FakeJetStream(waiting=0))

  assert report.verdict == "fail"
  assert "SignalWorker is not running" in report.detail


async def test_a_missing_stream_and_a_missing_consumer_are_worded_apart():
  no_stream = await status.probe_signal_stream(FakeJetStream(stream_missing=True))
  no_consumer = await status.probe_signal_stream(FakeJetStream(consumer_missing=True))

  assert no_stream.verdict == no_consumer.verdict == "fail"
  assert "no SIGNALS stream" in no_stream.detail
  assert "does not exist" in no_consumer.detail


# ── The screen and the exit code ───────────────────────────────────────────


def test_only_a_failed_link_fails_the_command():
  warned = [
    LinkReport("NATS", "ok", "up"),
    LinkReport("Workers", "warn", "0 connected"),
  ]

  assert status.exit_code(warned) == 0
  assert status.exit_code([*warned, LinkReport("JetStream", "fail", "down")]) == 1


def test_a_listing_is_indented_under_its_detail_column():
  screen = status.render_reports(
    [
      LinkReport("NATS", "ok", "nats:4222"),
      LinkReport("Workers", "warn", "0 connected", ("offline  FOREX-MT5-1",)),
    ]
  )

  assert screen.splitlines() == [
    "  [ OK ] NATS     nats:4222",
    "  [WARN] Workers  0 connected",
    "                  offline  FOREX-MT5-1",
  ]


def test_the_api_probe_never_prints_the_secret_prefix(monkeypatch):
  import asyncio

  monkeypatch.setattr(status.settings.broker_api, "API_PREFIX", "hidden-prefix")
  monkeypatch.setattr(status.settings.webhook, "PORT", 1)

  report = asyncio.run(status.probe_api())

  assert report.verdict == "fail"
  assert "hidden-prefix" not in report.detail
