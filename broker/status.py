"""
broker/status.py — ``make status``: is every link the broker depends on up.

The broker sits in the middle of three conversations and each one fails
quietly: a producer that stopped publishing looks like a quiet market, a worker
that dropped off NATS looks like a signal nobody wanted, and a ``SignalWorker``
that stopped pulling leaves alerts stored on JetStream while the webhook keeps
answering ``202``. This module asks each link once and prints the lot on one
screen:

* **PostgreSQL**, **NATS** and the **Broker API** — the stack's own services.
* **JetStream** — the ``SIGNALS`` stream exists, and the broker's durable
  consumer has a pull request waiting, i.e. ``SignalWorker`` is running.
* **Engine (QTE)** and **Workers** — other machines. Neither has a ping in its
  contract, so nothing is sent to them. The NATS server's own connection list
  (``/connz`` on the monitoring port) is read instead: QTE names its
  connection ``qte-broker-sink``, and a worker is the client subscribed to its
  private ``ADMIN.<market>.<gateway>.<account_id>`` subject. Connected workers
  are then set against the ``accounts`` table, so an account that has gone
  missing is listed as offline rather than simply absent.

Every probe is read-only: nothing is published and nothing is written, so it is
safe against a live deployment.

Only the stack's own services decide the exit code. A missing engine or worker
is a warning: a deployment fed by TradingView alone has no engine, and an
account may be offline on purpose.

Run inside the broker container (``make status`` does), where ``nats`` and
``postgres`` resolve and the monitoring port is not published to the world.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Sequence

import httpx
import nats as nats_lib
from nats.aio.client import Client as NATSClient
from nats.js.errors import NotFoundError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from broker.nats import JETSTREAM_SIGNAL_STREAM
from broker.schemas.publisher_schema import PublishTopicEnum
from broker.settings import settings

Verdict = Literal["ok", "warn", "fail"]

# The name Quant-Trading-Engine gives the NATS connection it publishes signals
# on (``qte_strategy_engine/broker_sink.py``). It is how the engine is told
# apart from a worker, so the two repositories have to agree on it.
ENGINE_CLIENT_NAME = "qte-broker-sink"

# This probe's own connection, so it can be left out of the client list.
STATUS_CLIENT_NAME = "broker-status"

CONNECT_TIMEOUT_SECONDS = 3
HTTP_TIMEOUT_SECONDS = 3.0

# A pull consumer has a request waiting almost all the time, but not while
# SignalWorker is busy with a message. Sampled a few times so one busy moment
# does not read as a stopped worker.
PULL_SAMPLE_COUNT = 4
PULL_SAMPLE_GAP_SECONDS = 0.4

_MARKERS: dict[Verdict, str] = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}

# First tokens of subscriptions that are plumbing rather than a strategy subject.
_RESERVED_SUBJECT_ROOTS = frozenset(
  {
    "_INBOX",
    PublishTopicEnum.ADMIN.value,
    PublishTopicEnum.SYSTEM.value,
    PublishTopicEnum.TRADE.value,
  }
)


@dataclass(frozen=True)
class LinkReport:
  """One link, what asking it showed, and any lines listed beneath it."""

  target: str
  verdict: Verdict
  detail: str
  listing: tuple[str, ...] = ()


@dataclass(frozen=True)
class KnownAccount:
  """One ``accounts`` row, reduced to what identifies its worker."""

  market: str
  gateway: str | None
  account_id: str
  account_name: str | None
  last_activity_at: datetime | None

  @property
  def worker_id(self) -> str:
    return f"{self.market}-{self.gateway or '?'}-{self.account_id}"


@dataclass(frozen=True)
class ConnectedWorker:
  """A NATS client holding a private ``ADMIN.<market>.<gateway>.<id>`` subject."""

  market: str
  gateway: str
  account_id: str
  address: str
  strategies: tuple[str, ...]

  @property
  def worker_id(self) -> str:
    return f"{self.market}-{self.gateway}-{self.account_id}"


@dataclass
class NatsClients:
  """The server's client list, sorted into who each connection is."""

  engines: list[dict[str, Any]] = field(default_factory=list)
  workers: list[ConnectedWorker] = field(default_factory=list)
  unidentified: list[dict[str, Any]] = field(default_factory=list)


def _describe(failure: BaseException) -> str:
  message = str(failure).strip()
  return f"{type(failure).__name__}: {message}" if message else type(failure).__name__


# ── The stack's own services ──────────────────────────────────────────────


async def probe_postgres() -> tuple[LinkReport, list[KnownAccount] | None]:
  """Answer ``SELECT 1`` and read the accounts the workers are set against.

  The accounts come back as ``None`` when they could not be read, which is not
  the same as an empty table: the worker report says so instead of calling
  every connected worker unknown.
  """
  postgres = settings.postgres
  endpoint = f"{postgres.HOST}:{postgres.PORT}/{postgres.DB}"
  # asyncpg waits a full minute for an unreachable host by default.
  engine = create_async_engine(
    settings.postgres_dsn, connect_args={"timeout": CONNECT_TIMEOUT_SECONDS}
  )
  try:
    async with engine.connect() as connection:
      await connection.execute(text("SELECT 1"))
      rows = await connection.execute(
        text(
          "SELECT market::text, gateway, account_id, account_name, last_activity_at "
          "FROM accounts ORDER BY market, gateway, account_id"
        )
      )
      accounts = [KnownAccount(*row) for row in rows]
  except Exception as failure:
    return LinkReport("PostgreSQL", "fail", f"{endpoint} ({_describe(failure)})"), None
  finally:
    await engine.dispose()
  return LinkReport(
    "PostgreSQL", "ok", f"{endpoint}, {len(accounts)} account(s)"
  ), accounts


async def probe_api() -> LinkReport:
  """The broker's own liveness route, on the port it listens on in this host."""
  webhook = settings.webhook
  api_prefix = (
    f"/{settings.broker_api.API_PREFIX}" if settings.broker_api.API_PREFIX else ""
  )
  host = "127.0.0.1" if webhook.HOST == "0.0.0.0" else webhook.HOST
  # The prefix is a secret path segment; it is requested, never printed.
  shown = f"GET {host}:{webhook.PORT}/v1/health"
  try:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
      response = await client.get(f"http://{host}:{webhook.PORT}{api_prefix}/v1/health")
  except Exception as failure:
    return LinkReport("Broker API", "fail", f"{shown} failed ({_describe(failure)})")
  if response.status_code != 200:
    return LinkReport("Broker API", "fail", f"{shown} answered {response.status_code}")
  return LinkReport("Broker API", "ok", f"{shown} answered 200")


async def probe_nats(accounts: list[KnownAccount] | None) -> list[LinkReport]:
  """NATS, its signal stream, and the engine and workers connected to it."""
  endpoint = f"{settings.nats.HOST}:{settings.nats.PORT}"
  options: dict[str, Any] = {
    "servers": [settings.nats_url],
    "name": STATUS_CLIENT_NAME,
    "connect_timeout": CONNECT_TIMEOUT_SECONDS,
    "allow_reconnect": False,
    # The failure is reported once, as this probe's own line — not as the
    # traceback nats-py prints for every attempt by default.
    "error_cb": _ignore_connection_error,
  }
  if settings.nats.TOKEN:
    options["token"] = settings.nats.TOKEN
  try:
    # nats-py retries the *initial* connect too, for minutes against a dead
    # server, so the whole dial is bounded here rather than per attempt.
    connection = await asyncio.wait_for(
      nats_lib.connect(**options), timeout=CONNECT_TIMEOUT_SECONDS + 1
    )
  except Exception as failure:
    unreachable = "not checked: NATS is unreachable"
    return [
      LinkReport("NATS", "fail", f"{endpoint} unreachable ({_describe(failure)})"),
      LinkReport("JetStream", "fail", unreachable),
      LinkReport("Engine (QTE)", "warn", unreachable),
      LinkReport("Workers", "warn", unreachable),
    ]
  try:
    stream_report = await probe_signal_stream(connection)
  finally:
    await connection.close()
  return [
    LinkReport("NATS", "ok", endpoint),
    stream_report,
    *await probe_clients(accounts),
  ]


async def _ignore_connection_error(failure: Exception) -> None:
  return None


async def probe_signal_stream(connection: NATSClient) -> LinkReport:
  """The ``SIGNALS`` stream, and whether ``SignalWorker`` is pulling from it."""
  jetstream = connection.jetstream()
  consumer_name = settings.jetstream.SIGNAL_CONSUMER
  try:
    stream = await jetstream.stream_info(JETSTREAM_SIGNAL_STREAM)
  except NotFoundError:
    return LinkReport(
      "JetStream",
      "fail",
      f"no {JETSTREAM_SIGNAL_STREAM} stream — the broker has not connected to "
      "this server yet",
    )
  except Exception as failure:
    return LinkReport("JetStream", "fail", _describe(failure))

  buffered = (
    f"{JETSTREAM_SIGNAL_STREAM} holds {stream.state.messages} undelivered signal(s)"
  )
  try:
    for sample in range(PULL_SAMPLE_COUNT):
      if sample:
        await asyncio.sleep(PULL_SAMPLE_GAP_SECONDS)
      consumer = await jetstream.consumer_info(JETSTREAM_SIGNAL_STREAM, consumer_name)
      if consumer.num_waiting:
        return LinkReport("JetStream", "ok", f"{buffered}; {consumer_name} is pulling")
  except NotFoundError:
    return LinkReport(
      "JetStream",
      "fail",
      f"{buffered}; consumer {consumer_name} does not exist — SignalWorker has "
      "never started",
    )
  except Exception as failure:
    return LinkReport("JetStream", "fail", f"{buffered}; {_describe(failure)}")
  return LinkReport(
    "JetStream",
    "fail",
    f"{buffered}; nothing is pulling from {consumer_name} — SignalWorker is not "
    "running, alerts are stored and not fanned out",
  )


# ── The engine and the workers ────────────────────────────────────────────


async def probe_clients(accounts: list[KnownAccount] | None) -> list[LinkReport]:
  """Who is connected to NATS right now, read from its monitoring endpoint."""
  monitor = f"{settings.nats.HOST}:{settings.nats.MONITOR_PORT}"
  try:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
      response = await client.get(f"http://{monitor}/connz", params={"subs": 1})
      response.raise_for_status()
      connections = response.json().get("connections") or []
  except Exception as failure:
    unverified = f"unverified — could not read {monitor}/connz ({_describe(failure)})"
    return [
      LinkReport("Engine (QTE)", "warn", unverified),
      LinkReport("Workers", "warn", unverified),
    ]
  clients = classify_connections(connections)
  reports = [engine_report(clients), workers_report(clients, accounts)]
  if clients.unidentified:
    reports.append(
      LinkReport(
        "Other clients",
        "ok",
        f"{len(clients.unidentified)} NATS client(s) that are neither",
        tuple(
          f"{entry.get('name') or 'unnamed'} ({entry.get('lang', '?')}) "
          f"from {entry.get('ip', '?')}"
          for entry in clients.unidentified
        ),
      )
    )
  return reports


def classify_connections(connections: Sequence[dict[str, Any]]) -> NatsClients:
  """Sort NATS connections into the engine, the workers and everything else.

  The broker's own connection is the one listening on ``TRADE`` — workers
  publish there and never subscribe — and is dropped along with this probe's.
  """
  clients = NatsClients()
  for entry in connections:
    subscriptions = entry.get("subscriptions_list") or []
    if entry.get("name") == STATUS_CLIENT_NAME:
      continue
    if PublishTopicEnum.TRADE.value in subscriptions:
      continue
    if entry.get("name") == ENGINE_CLIENT_NAME:
      clients.engines.append(entry)
      continue
    private_subjects = [
      tokens
      for tokens in (subject.split(".") for subject in subscriptions)
      if len(tokens) == 4 and tokens[0] == PublishTopicEnum.ADMIN.value
    ]
    if not private_subjects:
      clients.unidentified.append(entry)
      continue
    strategies = tuple(
      sorted(
        subject
        for subject in subscriptions
        if not subject.startswith("$")
        and subject.split(".")[0] not in _RESERVED_SUBJECT_ROOTS
      )
    )
    for _, market, gateway, account_id in private_subjects:
      clients.workers.append(
        ConnectedWorker(
          market=market,
          gateway=gateway,
          account_id=account_id,
          address=str(entry.get("ip", "?")),
          strategies=strategies,
        )
      )
  return clients


def engine_report(clients: NatsClients) -> LinkReport:
  if not clients.engines:
    return LinkReport(
      "Engine (QTE)",
      "warn",
      f"no NATS client named {ENGINE_CLIENT_NAME} — the engine's runner is down, "
      "cannot reach this server, or delivers over HTTP",
    )
  addresses = ", ".join(str(entry.get("ip", "?")) for entry in clients.engines)
  return LinkReport(
    "Engine (QTE)", "ok", f"connected as {ENGINE_CLIENT_NAME} from {addresses}"
  )


def workers_report(
  clients: NatsClients, accounts: list[KnownAccount] | None
) -> LinkReport:
  """Connected workers, and the known accounts with no worker connected."""
  online = [
    f"online   {worker.worker_id}  from {worker.address}  "
    f"strategies: {', '.join(worker.strategies) or 'none'}"
    for worker in sorted(clients.workers, key=lambda worker: worker.worker_id)
  ]
  if accounts is None:
    summary = f"{len(clients.workers)} connected; accounts table not read"
    return LinkReport(
      "Workers", "warn" if not clients.workers else "ok", summary, tuple(online)
    )

  missing = [
    account for account in accounts if not _is_connected(account, clients.workers)
  ]
  offline = [
    f"offline  {account.worker_id}"
    + (f"  ({account.account_name})" if account.account_name else "")
    + f"  last activity {_when(account.last_activity_at)}"
    for account in missing
  ]
  summary = f"{len(clients.workers)} connected, {len(missing)} known account(s) offline"
  if not clients.workers:
    summary += " — a signal published now reaches nobody"
  verdict: Verdict = "ok" if clients.workers and not missing else "warn"
  return LinkReport("Workers", verdict, summary, (*online, *offline))


def _is_connected(account: KnownAccount, workers: Sequence[ConnectedWorker]) -> bool:
  """Whether a connected worker serves *account*.

  An account row written before its worker ever announced a gateway has none,
  so it is matched on market and id alone.
  """
  for worker in workers:
    if worker.account_id != account.account_id:
      continue
    if worker.market.upper() != account.market.upper():
      continue
    if account.gateway is None or worker.gateway.upper() == account.gateway.upper():
      return True
  return False


def _when(moment: datetime | None) -> str:
  return moment.strftime("%Y-%m-%d %H:%M %Z").strip() if moment else "never"


# ── The whole picture ─────────────────────────────────────────────────────


async def collect_reports() -> list[LinkReport]:
  """Ask every link; the order returned is the order printed."""
  (postgres_report, accounts), api_report = await asyncio.gather(
    probe_postgres(), probe_api()
  )
  nats_reports = await probe_nats(accounts)
  nats_report, stream_report, *client_reports = nats_reports
  return [postgres_report, nats_report, stream_report, api_report, *client_reports]


def render_reports(reports: Sequence[LinkReport]) -> str:
  width = max(len(report.target) for report in reports)
  lines: list[str] = []
  for report in reports:
    lines.append(
      f"  {_MARKERS[report.verdict]} {report.target.ljust(width)}  {report.detail}"
    )
    lines.extend(f"  {' ' * (width + 9)}{entry}" for entry in report.listing)
  return "\n".join(lines)


def exit_code(reports: Sequence[LinkReport]) -> int:
  """Non-zero when any link is down; a warning alone does not fail the run."""
  return 1 if any(report.verdict == "fail" for report in reports) else 0


def main() -> None:
  reports = asyncio.run(collect_reports())
  print("Broker status")
  print(render_reports(reports))
  raise SystemExit(exit_code(reports))


if __name__ == "__main__":
  main()
