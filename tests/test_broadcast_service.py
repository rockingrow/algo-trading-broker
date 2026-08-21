"""Tests for the signal-cycle broadcast (broker.services.broadcast_service).

The feature has two halves and they are tested as such:

* the **writer** (``SignalBroadcastService``) only touches the database — a
  signal or a worker's TRADE report records a change and appends a write-log
  entry, and never calls Telegram;
* the **dispatcher** (``BroadcastDispatcher``) reads that log back and edits the
  one message each chat holds for the trade.

The fake repository below keeps the same invariants the real one does (a
per-cycle ``last_seq``, a pending/sending/delivered log, per-chat
``delivered_seq``) so the tests exercise the real sequencing rules rather than a
simplified model of them.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from broker.constants import (
  NOTIFICATION_INCLUDE_SIGNAL_RAW,
  NOTIFICATION_TIMEZONE_KEY,
  PRIVATE_REPLY_NOTIFY_KEY,
  PUBLIC_BROADCAST_CHAT_IDS_KEY,
  PUBLIC_REPLY_NOTIFY_KEY,
  SILENT_SIGNAL,
)
from broker.domain.broadcast_status import merge_status, status_for_action
from broker.helpers import emoji_constants as em
from broker.interfaces import BroadcastCycleView
from broker.schemas.account_schema import MarketTypeEnum
from broker.schemas.core import (
  BroadcastAudienceEnum,
  BroadcastLogKindEnum,
  BroadcastLogStatusEnum,
  BroadcastStatusEnum,
  SignalActionEnum,
)
from broker.schemas.trade_event_schema import PositionEvent
from broker.schemas.trade_schema import TradeStatusEnum
from broker.schemas.webhook_schema import (
  IndicatorsSchema,
  InputsSchema,
  PositionSchema,
  WebhookPayload,
)
from broker.services import broadcast_service as bs
from broker.services.broadcast_service import (
  BroadcastDispatcher,
  SignalBroadcastService,
  build_broadcast_event,
)
from broker.services.notification_service import EditOutcome


@pytest.fixture(autouse=True)
def telegram_on(monkeypatch):
  """The writer records nothing while Telegram is switched off."""
  monkeypatch.setattr(bs.settings.telegram, "ENABLED", True)


# ── Fakes ───────────────────────────────────────────────────────────


class FakeBroadcastRepository:
  """In-memory stand-in keyed the same way the tables are."""

  def __init__(self):
    self.cycles: dict[tuple[str, str], SimpleNamespace] = {}
    self.by_id: dict[uuid.UUID, SimpleNamespace] = {}
    self.chats: dict[uuid.UUID, dict[str, SimpleNamespace]] = {}
    self.workers: dict[uuid.UUID, dict[str, SimpleNamespace]] = {}
    self.logs: list[SimpleNamespace] = []
    self.marked: list[uuid.UUID] = []
    self.fail_record = False

  # -- writers --
  def _append_log(self, cycle, kind, payload) -> None:
    cycle.last_seq += 1
    self.logs.append(
      SimpleNamespace(
        id=uuid.uuid4(),
        broadcast_message_id=cycle.id,
        seq=cycle.last_seq,
        kind=kind,
        payload=payload,
        status=BroadcastLogStatusEnum.PENDING,
        attempts=0,
        last_error=None,
      )
    )

  async def record_event(
    self, *, strategy, signal_uxid, symbol, timeframe, action, event
  ):
    if self.fail_record:
      return None
    key = (strategy, signal_uxid)
    cycle = self.cycles.get(key)
    incoming = status_for_action(action)
    if cycle is None:
      cycle = SimpleNamespace(
        id=uuid.uuid4(),
        strategy=strategy,
        signal_uxid=signal_uxid,
        symbol=symbol,
        timeframe=timeframe,
        actions=action.value,
        latest_action=action,
        status=incoming,
        events=[event],
        last_seq=0,
      )
      self.cycles[key] = cycle
      self.by_id[cycle.id] = cycle
      self.chats[cycle.id] = {}
      self.workers[cycle.id] = {}
      self._append_log(cycle, BroadcastLogKindEnum.SIGNAL, event)
      return cycle

    last = cycle.events[-1] if cycle.events else {}
    if (last.get("action"), last.get("timestamp")) == (
      event.get("action"),
      event.get("timestamp"),
    ):
      return cycle  # replay

    cycle.events = list(cycle.events) + [event]
    cycle.actions = f"{cycle.actions},{action.value}"
    cycle.latest_action = action
    cycle.status = merge_status(cycle.status, incoming)
    self._append_log(cycle, BroadcastLogKindEnum.SIGNAL, event)
    return cycle

  async def record_worker_execution(
    self,
    *,
    strategy,
    signal_uxid,
    worker_id,
    account_id,
    market,
    gateway,
    latest_status,
    latest_action=None,
    reject_reason=None,
    event_at=None,
  ):
    cycle = self.cycles.get((strategy, signal_uxid))
    if cycle is None:
      return None
    workers = self.workers.setdefault(cycle.id, {})
    worker = workers.get(worker_id)
    if worker is not None and (worker.latest_status, worker.latest_action) == (
      latest_status,
      latest_action,
    ):
      return cycle
    workers[worker_id] = SimpleNamespace(
      worker_id=worker_id,
      account_id=account_id,
      market=market,
      gateway=gateway,
      latest_status=latest_status,
      latest_action=latest_action,
      reject_reason=reject_reason,
    )
    self._append_log(
      cycle,
      BroadcastLogKindEnum.EXECUTION,
      {
        "worker_id": worker_id,
        "status": getattr(latest_status, "value", latest_status),
      },
    )
    return cycle

  # -- dispatcher side --
  async def claim_pending_logs(self, broadcast_message_id, *, max_attempts):
    claimed = [
      entry
      for entry in self.logs
      if entry.broadcast_message_id == broadcast_message_id
      and entry.status == BroadcastLogStatusEnum.PENDING
      and entry.attempts < max_attempts
    ]
    for entry in claimed:
      entry.status = BroadcastLogStatusEnum.SENDING
      entry.attempts += 1
    return sorted(claimed, key=lambda e: e.seq)

  async def finish_logs(self, log_ids, *, delivered, error=None, max_attempts=None):
    for entry in self.logs:
      if entry.id not in log_ids:
        continue
      if delivered:
        entry.status = BroadcastLogStatusEnum.DELIVERED
        continue
      entry.last_error = error
      entry.status = (
        BroadcastLogStatusEnum.FAILED
        if max_attempts is not None and entry.attempts >= max_attempts
        else BroadcastLogStatusEnum.PENDING
      )
    return True

  async def list_cycles_with_pending_logs(
    self, *, max_attempts, stale_after_seconds, limit=50
  ):
    ids = [
      entry.broadcast_message_id
      for entry in self.logs
      if entry.status == BroadcastLogStatusEnum.PENDING
      and entry.attempts < max_attempts
    ]
    return list(dict.fromkeys(ids))[:limit]

  async def reclaim_stale_logs(self, *, stale_after_seconds):
    return 0

  async def load_cycle(self, broadcast_message_id):
    cycle = self.by_id.get(broadcast_message_id)
    if cycle is None:
      return None
    return BroadcastCycleView(
      message=cycle,
      chats=list(self.chats.get(cycle.id, {}).values()),
      workers=list(self.workers.get(cycle.id, {}).values()),
    )

  async def upsert_chat(
    self,
    broadcast_message_id,
    *,
    audience,
    chat_id,
    message_id,
    message,
    delivered_seq=None,
    notified_event_count=None,
    last_error=None,
  ):
    rows = self.chats.setdefault(broadcast_message_id, {})
    row = rows.get(chat_id)
    if row is None:
      rows[chat_id] = SimpleNamespace(
        audience=audience,
        chat_id=chat_id,
        message_id=message_id,
        message=message,
        delivered_seq=delivered_seq or 0,
        notified_event_count=notified_event_count,
        last_error=last_error,
      )
      return True
    row.audience = audience
    if message_id is not None:
      row.message_id = message_id
    if message is not None:
      row.message = message
    if delivered_seq is not None and delivered_seq > row.delivered_seq:
      row.delivered_seq = delivered_seq
    if notified_event_count is not None and notified_event_count > (
      row.notified_event_count or 0
    ):
      row.notified_event_count = notified_event_count
    row.last_error = last_error
    return True

  async def mark_broadcast(self, broadcast_message_id):
    self.marked.append(broadcast_message_id)
    return True


class FakeTelegram:
  """Records sends and edits; ``edit_outcome`` drives the edit result."""

  def __init__(
    self,
    edit_outcome: EditOutcome = EditOutcome.OK,
    send_ok: bool = True,
    enabled: bool = True,
    reply_ok: bool = True,
  ):
    self.sent: list[tuple[str, str]] = []
    self.edited: list[tuple[str, str, str]] = []
    # (chat id, message replied to, body) of every update notice.
    self.replied: list[tuple[str, str, str]] = []
    self.edit_outcome = edit_outcome
    self.send_ok = send_ok
    self.enabled = enabled
    self.reply_ok = reply_ok
    self._next_id = 100

  async def send_and_get_message_id(self, chat, text):
    # BroadcastNotifier hands us a ChatTarget so it can also carry the
    # supergroup-topic id — the tests only ever address plain chats, so we
    # keep asserting on ``chat.chat_id`` for readability.
    self.sent.append((chat.chat_id, text))
    if not self.send_ok:
      return None
    self._next_id += 1
    return str(self._next_id)

  async def edit_message(self, chat, message_id, text):
    self.edited.append((chat.chat_id, message_id, text))
    return self.edit_outcome

  async def reply_message(self, chat, reply_to_message_id, text):
    self.replied.append((chat.chat_id, reply_to_message_id, text))
    return self.reply_ok


class FakeListener:
  """Stands in for the Postgres LISTEN connection."""

  def __init__(self):
    self.started = False
    self.stopped = False

  async def start(self):
    self.started = True

  async def stop(self):
    self.stopped = True


class FakeSettingRepository:
  def __init__(self, **values):
    self.values = dict(values)

  async def get(self, key):
    return self.values.get(key)

  async def get_many(self, keys):
    return {k: v for k, v in self.values.items() if k in keys}

  async def set(self, key, value):
    self.values[key] = value
    return True


class FakeSignalRepository:
  """Only ``get_by_id`` matters here: it is how a TRADE event finds its cycle."""

  def __init__(self, rows: dict | None = None):
    self.rows = rows if rows is not None else {}
    self.lookups: list[str] = []

  async def get_by_id(self, signal_id):
    self.lookups.append(signal_id)
    return self.rows.get(signal_id)


def _payload(action=SignalActionEnum.LONG, uxid="9f2c4b7e18a3d605", **overrides):
  base = dict(
    strategy="strat",
    symbol="XAUUSD",
    timeframe="60",
    timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
    signal_uxid=uxid,
    position=PositionSchema(action=action, price=100.0, quantity=1.0, sl=95.0),
    token="secret",
  )
  base.update(overrides)
  return WebhookPayload(**base)


def _trade_event(**overrides) -> PositionEvent:
  base = dict(
    event="CREATED",
    market="FOREX",
    strategy="strat",
    id=1,
    # A worker echoes back the per-signal id it was given on the SIGNAL
    # payload; the broker reads that row to find the cycle behind it.
    signal_id="sig-1",
    ref_source_id="r1",
    ref_id="r1",
    symbol="XAUUSD",
    action="LONG",
    volume=0.1,
    opened_price=100.0,
    status="OPENED",
    account_id="12345678",
    gateway="MT5",
  )
  base.update(overrides)
  return PositionEvent(**base)


def _signal_row(strategy="strat", uxid="9f2c4b7e18a3d605"):
  return SimpleNamespace(strategy=strategy, signal_uxid=uxid)


def _writer(repo=None, signals=None):
  repo = repo if repo is not None else FakeBroadcastRepository()
  return (
    SignalBroadcastService(
      repository=repo,
      signal_repository=signals
      if signals is not None
      else FakeSignalRepository({"sig-1": _signal_row()}),
    ),
    repo,
  )


def _dispatcher(
  repo, *, private=("-100",), public="", telegram=None, settings_values=None
):
  channel = telegram or FakeTelegram()
  values = dict(settings_values or {})
  values.setdefault(PUBLIC_BROADCAST_CHAT_IDS_KEY, public)
  dispatcher = BroadcastDispatcher(
    repository=repo,
    setting_repository=FakeSettingRepository(**values),
    notifier=channel,
    listener=FakeListener(),
    private_chat_ids=list(private),
  )
  return dispatcher, channel


async def _run(payload, repo=None, **dispatcher_kwargs):
  """Record a signal and immediately dispatch its cycle."""
  writer, repo = _writer(repo)
  await writer.broadcast(payload)
  cycle = repo.cycles[(payload.strategy, payload.signal_uxid)]
  dispatcher, channel = _dispatcher(repo, **dispatcher_kwargs)
  await dispatcher.dispatch(cycle.id)
  return repo, channel, cycle


def _cycle_of(repo, strategy="strat", uxid="9f2c4b7e18a3d605"):
  return repo.cycles[(strategy, uxid)]


def _only_cycle(repo):
  """The single cycle a test recorded, without spelling out its key."""
  return next(iter(repo.cycles.values()))


# ── Writer: signals ─────────────────────────────────────────────────


async def test_signal_records_a_cycle_and_a_log_entry():
  writer, repo = _writer()
  await writer.broadcast(_payload())

  cycle = _cycle_of(repo)
  assert cycle.events[0]["action"] == "LONG"
  assert [entry.kind for entry in repo.logs] == [BroadcastLogKindEnum.SIGNAL]
  assert repo.logs[0].seq == cycle.last_seq == 1


async def test_writer_never_calls_telegram():
  """The whole point of the split: a Telegram outage cannot delay a signal."""
  writer, repo = _writer()
  _, channel = _dispatcher(repo)
  await writer.broadcast(_payload())
  assert channel.sent == []
  assert channel.edited == []


async def test_writer_is_a_noop_when_telegram_is_disabled(monkeypatch):
  monkeypatch.setattr(bs.settings.telegram, "ENABLED", False)
  writer, repo = _writer()
  await writer.broadcast(_payload())
  assert repo.cycles == {}


async def test_follow_up_signal_appends_to_the_same_cycle():
  writer, repo = _writer()
  await writer.broadcast(_payload())
  await writer.broadcast(_payload(action=SignalActionEnum.TP1))

  assert len(repo.cycles) == 1
  assert _cycle_of(repo).actions == "LONG,TP1"
  assert len(repo.logs) == 2


async def test_a_different_uxid_starts_its_own_cycle():
  writer, repo = _writer()
  await writer.broadcast(_payload())
  await writer.broadcast(_payload(uxid="0000111122223333"))
  assert len(repo.cycles) == 2


async def test_same_uxid_on_another_strategy_is_a_separate_cycle():
  """The unique key is the pair, so two strategies may reuse an id."""
  writer, repo = _writer()
  await writer.broadcast(_payload())
  await writer.broadcast(_payload(strategy="other"))
  assert len(repo.cycles) == 2


# ── Writer: worker executions ───────────────────────────────────────


async def test_trade_event_records_the_worker_on_the_cycle():
  writer, repo = _writer()
  await writer.broadcast(_payload())

  await writer.record_execution(_trade_event())

  workers = repo.workers[_cycle_of(repo).id]
  assert list(workers) == ["FOREX-MT5-12345678"]
  assert workers["FOREX-MT5-12345678"].latest_status == TradeStatusEnum.OPENED
  assert repo.logs[-1].kind == BroadcastLogKindEnum.EXECUTION


async def test_the_cycle_is_found_through_the_echoed_signal_id():
  """The event carries the per-signal id, so the signals row is what maps it
  onto a cycle."""
  signals = FakeSignalRepository({"sig-1": _signal_row()})
  writer, repo = _writer(signals=signals)
  await writer.broadcast(_payload())

  await writer.record_execution(_trade_event())

  assert signals.lookups == ["sig-1"]
  assert repo.workers[_cycle_of(repo).id]


async def test_trade_event_without_a_signal_id_is_ignored():
  """A manual trade, or a worker too old to echo the id, has no cycle."""
  writer, repo = _writer()
  await writer.broadcast(_payload())
  await writer.record_execution(_trade_event(signal_id=None))
  assert repo.workers[_cycle_of(repo).id] == {}


async def test_trade_event_for_an_unknown_signal_is_ignored():
  """A worker can report a trade for a signal that was never broadcast."""
  writer, repo = _writer(signals=FakeSignalRepository({}))
  await writer.broadcast(_payload())
  await writer.record_execution(_trade_event())
  assert repo.workers[_cycle_of(repo).id] == {}


async def test_trade_event_with_an_unmappable_status_is_ignored():
  writer, repo = _writer()
  await writer.broadcast(_payload())
  await writer.record_execution(_trade_event(status="SOMETHING_ELSE"))
  assert repo.workers[_cycle_of(repo).id] == {}


# ── Dispatcher: first delivery ──────────────────────────────────────


async def test_entry_sends_one_message_per_chat():
  repo, channel, cycle = await _run(_payload(), private=("-100", "-200"), public="-300")

  assert [chat_id for chat_id, _ in channel.sent] == ["-100", "-200", "-300"]
  assert channel.edited == []
  assert repo.marked == [cycle.id]


async def test_delivery_stores_message_id_body_and_sequence():
  repo, channel, cycle = await _run(_payload())

  chat = repo.chats[cycle.id]["-100"]
  assert chat.message_id == "101"
  assert chat.audience == BroadcastAudienceEnum.PRIVATE
  assert "<b>XAUUSD</b>" in chat.message
  assert chat.delivered_seq == cycle.last_seq == 1


async def test_public_chats_are_tagged_as_public():
  repo, _, cycle = await _run(_payload(), private=(), public="-300")
  assert repo.chats[cycle.id]["-300"].audience == BroadcastAudienceEnum.PUBLIC


async def test_a_chat_listed_in_both_audiences_is_broadcast_once():
  _, channel, _ = await _run(_payload(), private=("-100",), public="-100")
  assert len(channel.sent) == 1


async def test_delivered_entries_are_marked_and_not_resent():
  repo, _, cycle = await _run(_payload())
  assert all(e.status == BroadcastLogStatusEnum.DELIVERED for e in repo.logs)

  dispatcher, channel = _dispatcher(repo)
  await dispatcher.dispatch(cycle.id)
  assert channel.sent == []


async def test_no_configured_chats_delivers_nothing_and_clears_the_log():
  """Recording still happens — a chat configured mid-trade picks up the full
  history — but there is nothing owed to anyone."""
  repo, channel, _ = await _run(_payload(), private=(), public="")
  assert channel.sent == []
  assert all(e.status == BroadcastLogStatusEnum.DELIVERED for e in repo.logs)


# ── Dispatcher: follow-ups edit the same message ────────────────────


async def test_follow_up_actions_edit_instead_of_sending():
  writer, repo = _writer()
  dispatcher, channel = _dispatcher(repo, private=("-100",), public="-300")

  await writer.broadcast(_payload())
  cycle = _cycle_of(repo)
  await dispatcher.dispatch(cycle.id)
  await writer.broadcast(_payload(action=SignalActionEnum.TP1))
  await dispatcher.dispatch(cycle.id)
  await writer.broadcast(_payload(action=SignalActionEnum.SL))
  await dispatcher.dispatch(cycle.id)

  # Two chats × one send each; every later action is an edit of those two.
  assert len(channel.sent) == 2
  assert [chat_id for chat_id, _, _ in channel.edited] == [
    "-100",
    "-300",
    "-100",
    "-300",
  ]


# ── Dispatcher: update notices ──────────────────────────────────────


def _notice_writer(repo=None):
  """Writer whose signal row resolves to the cycle ``_payload()`` opens.

  The worker path finds a cycle through the signal row's ``signal_uxid``, and
  these tests mix signals and worker executions on one trade.
  """
  return _writer(
    repo,
    signals=FakeSignalRepository({"sig-1": _signal_row(uxid=_payload().signal_uxid)}),
  )


async def test_the_first_message_carries_no_notice():
  """Nothing to announce yet — the message that just went out *is* the news."""
  repo, channel, cycle = await _run(_payload(), private=("-100",), public="-300")

  assert channel.replied == []
  assert repo.chats[cycle.id]["-100"].notified_event_count == 1


async def test_a_follow_up_replies_to_the_edited_message_in_every_audience():
  """An edit is silent, so each new action also gets a two-line reply."""
  writer, repo = _notice_writer()
  dispatcher, channel = _dispatcher(repo, private=("-100",), public="-300")

  await writer.broadcast(_payload())
  cycle = _only_cycle(repo)
  await dispatcher.dispatch(cycle.id)
  await writer.broadcast(_payload(action=SignalActionEnum.TP1))
  await dispatcher.dispatch(cycle.id)

  private_message_id = repo.chats[cycle.id]["-100"].message_id
  public_message_id = repo.chats[cycle.id]["-300"].message_id
  assert channel.replied == [
    ("-100", private_message_id, f"[{em.CYCLE_RUNNING}RUNNING]\n{em.TP1} TP1"),
    ("-300", public_message_id, f"[{em.CYCLE_RUNNING}RUNNING]\n{em.TP1} TP1"),
  ]


async def test_a_closing_action_is_announced_as_closed():
  writer, repo = _notice_writer()
  dispatcher, channel = _dispatcher(repo)

  await writer.broadcast(_payload())
  cycle = _only_cycle(repo)
  await dispatcher.dispatch(cycle.id)
  await writer.broadcast(_payload(action=SignalActionEnum.TP2))
  await dispatcher.dispatch(cycle.id)

  assert channel.replied[-1][2] == f"[{em.CYCLE_CLOSED}CLOSED]\n{em.TP2} TP2"


async def test_events_delivered_together_get_one_notice_each():
  """A dispatch coalescing two actions still tells the chat about both, and
  the intermediate one is not back-dated to the cycle's final status."""
  writer, repo = _notice_writer()
  dispatcher, channel = _dispatcher(repo)

  await writer.broadcast(_payload())
  cycle = _only_cycle(repo)
  await dispatcher.dispatch(cycle.id)
  await writer.broadcast(_payload(action=SignalActionEnum.TP1))
  await writer.broadcast(_payload(action=SignalActionEnum.TP2))
  await dispatcher.dispatch(cycle.id)

  assert [text for _, _, text in channel.replied] == [
    f"[{em.CYCLE_RUNNING}RUNNING]\n{em.TP1} TP1",
    f"[{em.CYCLE_CLOSED}CLOSED]\n{em.TP2} TP2",
  ]


async def test_a_worker_execution_alone_announces_nothing():
  """The worker table changes the body, but no new action happened."""
  writer, repo = _notice_writer()
  dispatcher, channel = _dispatcher(repo)

  await writer.broadcast(_payload())
  cycle = _only_cycle(repo)
  await dispatcher.dispatch(cycle.id)
  await writer.record_execution(_trade_event())
  await dispatcher.dispatch(cycle.id)

  assert channel.edited  # the body did change
  assert channel.replied == []


async def test_a_redelivery_does_not_repeat_a_notice():
  writer, repo = _notice_writer()
  dispatcher, channel = _dispatcher(repo)

  await writer.broadcast(_payload())
  cycle = _only_cycle(repo)
  await dispatcher.dispatch(cycle.id)
  await writer.broadcast(_payload(action=SignalActionEnum.TP1))
  await dispatcher.dispatch(cycle.id)
  # A sweep finding nothing new must not announce TP1 a second time.
  await writer.record_execution(_trade_event())
  await dispatcher.dispatch(cycle.id)

  assert len(channel.replied) == 1


async def test_a_failed_notice_is_retried_on_the_next_pass():
  writer, repo = _notice_writer()
  channel = FakeTelegram(reply_ok=False)
  dispatcher, _ = _dispatcher(repo, telegram=channel)

  await writer.broadcast(_payload())
  cycle = _only_cycle(repo)
  await dispatcher.dispatch(cycle.id)
  await writer.broadcast(_payload(action=SignalActionEnum.TP1))
  await dispatcher.dispatch(cycle.id)

  assert repo.chats[cycle.id]["-100"].notified_event_count == 1
  channel.reply_ok = True
  await writer.record_execution(_trade_event())
  await dispatcher.dispatch(cycle.id)
  assert [text for _, _, text in channel.replied][-1].endswith(f"{em.TP1} TP1")
  assert repo.chats[cycle.id]["-100"].notified_event_count == 2


async def test_a_chat_from_before_notices_existed_is_caught_up_silently():
  """A row with no count cannot know what it announced; replaying the whole
  trade into the channel would be worse than staying quiet once."""
  writer, repo = _notice_writer()
  dispatcher, channel = _dispatcher(repo)

  await writer.broadcast(_payload())
  cycle = _only_cycle(repo)
  await dispatcher.dispatch(cycle.id)
  repo.chats[cycle.id]["-100"].notified_event_count = None

  await writer.broadcast(_payload(action=SignalActionEnum.TP1))
  await dispatcher.dispatch(cycle.id)

  assert channel.replied == []
  assert repo.chats[cycle.id]["-100"].notified_event_count == 2


async def test_reply_notify_disabled_for_private_skips_only_that_audience():
  """Turning the private toggle off silences its reply; public is unaffected,
  and the message body itself is still edited for both."""
  writer, repo = _notice_writer()
  dispatcher, channel = _dispatcher(
    repo,
    private=("-100",),
    public="-300",
    settings_values={PRIVATE_REPLY_NOTIFY_KEY: "0"},
  )

  await writer.broadcast(_payload())
  cycle = _only_cycle(repo)
  await dispatcher.dispatch(cycle.id)
  await writer.broadcast(_payload(action=SignalActionEnum.TP1))
  await dispatcher.dispatch(cycle.id)

  assert [chat_id for chat_id, _, _ in channel.replied] == ["-300"]
  assert {chat_id for chat_id, _, _ in channel.edited} == {"-100", "-300"}
  # The skipped notice is still tracked as delivered, so re-enabling later
  # does not replay the trade's backlog into the chat.
  assert repo.chats[cycle.id]["-100"].notified_event_count == 2


async def test_reply_notify_disabled_for_public_skips_only_that_audience():
  writer, repo = _notice_writer()
  dispatcher, channel = _dispatcher(
    repo,
    private=("-100",),
    public="-300",
    settings_values={PUBLIC_REPLY_NOTIFY_KEY: "0"},
  )

  await writer.broadcast(_payload())
  cycle = _only_cycle(repo)
  await dispatcher.dispatch(cycle.id)
  await writer.broadcast(_payload(action=SignalActionEnum.TP1))
  await dispatcher.dispatch(cycle.id)

  assert [chat_id for chat_id, _, _ in channel.replied] == ["-100"]
  assert repo.chats[cycle.id]["-300"].notified_event_count == 2


async def test_a_failed_edit_announces_nothing():
  """The message the notice points at was not updated — do not advertise it."""
  writer, repo = _notice_writer()
  channel = FakeTelegram()
  dispatcher, _ = _dispatcher(repo, telegram=channel)

  await writer.broadcast(_payload())
  cycle = _only_cycle(repo)
  await dispatcher.dispatch(cycle.id)
  channel.edit_outcome = EditOutcome.FAILED
  await writer.broadcast(_payload(action=SignalActionEnum.TP1))
  await dispatcher.dispatch(cycle.id)

  assert channel.replied == []


async def test_edited_body_accumulates_the_whole_cycle():
  writer, repo = _writer()
  dispatcher, channel = _dispatcher(repo)

  await writer.broadcast(_payload())
  cycle = _cycle_of(repo)
  await dispatcher.dispatch(cycle.id)
  await writer.broadcast(_payload(action=SignalActionEnum.TP1))
  await dispatcher.dispatch(cycle.id)

  _, _, text = channel.edited[-1]
  assert "<b>LONG</b>" in text
  assert "<b>TP1</b>" in text


async def test_closing_action_flips_the_cycle_to_closed():
  writer, repo = _writer()
  dispatcher, channel = _dispatcher(repo)

  await writer.broadcast(_payload())
  cycle = _cycle_of(repo)
  await dispatcher.dispatch(cycle.id)
  await writer.broadcast(_payload(action=SignalActionEnum.TP2))
  await dispatcher.dispatch(cycle.id)

  assert cycle.status == BroadcastStatusEnum.CLOSED
  assert "<b>CLOSED</b>" in channel.edited[-1][2]


async def test_tp1_keeps_the_cycle_running():
  writer, repo = _writer()
  await writer.broadcast(_payload())
  await writer.broadcast(_payload(action=SignalActionEnum.TP1))
  assert _cycle_of(repo).status == BroadcastStatusEnum.RUNNING


# ── Dispatcher: the private execution table ─────────────────────────


async def test_private_body_carries_the_worker_table():
  writer, repo = _writer()
  await writer.broadcast(_payload())
  await writer.record_execution(_trade_event())

  dispatcher, channel = _dispatcher(repo, private=("-100",), public="-300")
  await dispatcher.dispatch(_cycle_of(repo).id)

  bodies = {chat_id: text for chat_id, text in channel.sent}
  assert "Executions (1)" in bodies["-100"]
  assert "MT5 ****5678" in bodies["-100"]
  assert "OPENED" in bodies["-100"]
  assert "Strategy:" in bodies["-100"]
  # The public copy stays the bare cycle body.
  assert "Executions" not in bodies["-300"]
  assert "Strategy:" not in bodies["-300"]


async def test_worker_status_change_edits_the_private_message():
  """The worker table only lives in the private copy, so that is what a
  worker-status-only change (no new signal) needs to re-edit."""
  writer, repo = _writer()
  dispatcher, channel = _dispatcher(repo, private=("-100",), public="")

  await writer.broadcast(_payload())
  cycle = _cycle_of(repo)
  await writer.record_execution(_trade_event())
  await dispatcher.dispatch(cycle.id)
  await writer.record_execution(_trade_event(status="TP2", event="UPDATED"))
  await dispatcher.dispatch(cycle.id)

  assert len(channel.sent) == 1
  assert "CLOSED" in channel.edited[-1][2]


async def test_repeated_worker_status_does_not_touch_telegram():
  writer, repo = _writer()
  dispatcher, channel = _dispatcher(repo, private=("-100",), public="")

  await writer.broadcast(_payload())
  cycle = _cycle_of(repo)
  await writer.record_execution(_trade_event())
  await dispatcher.dispatch(cycle.id)
  await writer.record_execution(_trade_event())  # same status again
  await dispatcher.dispatch(cycle.id)

  assert channel.edited == []


# ── Dispatcher: sequencing and failure handling ─────────────────────


async def test_a_chat_already_at_this_sequence_is_skipped():
  """The guard that makes an out-of-order delivery harmless."""
  repo, _, cycle = await _run(_payload())
  repo.logs[0].status = BroadcastLogStatusEnum.PENDING  # replay the same entry

  dispatcher, channel = _dispatcher(repo)
  await dispatcher.dispatch(cycle.id)

  assert channel.edited == []
  assert channel.sent == []


async def test_a_deleted_message_is_resent():
  repo, _, cycle = await _run(_payload())
  writer, _ = _writer(repo)
  await writer.broadcast(_payload(action=SignalActionEnum.TP1))

  dispatcher, channel = _dispatcher(
    repo, telegram=FakeTelegram(edit_outcome=EditOutcome.MISSING)
  )
  await dispatcher.dispatch(cycle.id)

  assert len(channel.edited) == 1
  assert len(channel.sent) == 1
  assert repo.chats[cycle.id]["-100"].message_id == "101"


async def test_a_transient_edit_failure_keeps_the_id_and_the_log_entry():
  """Re-sending on a rate limit would duplicate the cycle in the chat, so the
  entry stays pending and the next pass edits again."""
  repo, _, cycle = await _run(_payload())
  writer, _ = _writer(repo)
  await writer.broadcast(_payload(action=SignalActionEnum.TP1))

  dispatcher, channel = _dispatcher(
    repo, telegram=FakeTelegram(edit_outcome=EditOutcome.FAILED)
  )
  await dispatcher.dispatch(cycle.id)

  assert channel.sent == []  # no duplicate message
  chat = repo.chats[cycle.id]["-100"]
  assert chat.message_id == "101"
  assert chat.last_error == "edit failed"
  assert repo.logs[-1].status == BroadcastLogStatusEnum.PENDING


async def test_a_failed_send_leaves_the_entry_pending_for_the_next_pass():
  writer, repo = _writer()
  await writer.broadcast(_payload())
  cycle = _cycle_of(repo)

  dispatcher, _ = _dispatcher(repo, telegram=FakeTelegram(send_ok=False))
  await dispatcher.dispatch(cycle.id)

  chat = repo.chats[cycle.id]["-100"]
  assert chat.message_id is None
  assert chat.last_error == "send failed"
  assert repo.logs[0].status == BroadcastLogStatusEnum.PENDING

  # Telegram recovers: the same entry is retried as a fresh send.
  dispatcher2, healthy = _dispatcher(repo)
  await dispatcher2.dispatch(cycle.id)
  assert len(healthy.sent) == 1
  assert repo.logs[0].status == BroadcastLogStatusEnum.DELIVERED


async def test_an_entry_that_used_up_its_attempts_is_failed():
  writer, repo = _writer()
  await writer.broadcast(_payload())
  cycle = _cycle_of(repo)

  dispatcher, _ = _dispatcher(repo, telegram=FakeTelegram(send_ok=False))
  for _ in range(dispatcher._max_attempts):
    await dispatcher.dispatch(cycle.id)

  assert repo.logs[0].status == BroadcastLogStatusEnum.FAILED


async def test_a_missing_cycle_clears_its_log_entries():
  writer, repo = _writer()
  await writer.broadcast(_payload())
  cycle = _cycle_of(repo)
  repo.by_id.pop(cycle.id)

  dispatcher, channel = _dispatcher(repo)
  await dispatcher.dispatch(cycle.id)

  assert channel.sent == []
  assert repo.logs[0].status == BroadcastLogStatusEnum.DELIVERED


async def test_unpersisted_cycle_records_nothing():
  repo = FakeBroadcastRepository()
  repo.fail_record = True
  writer, _ = _writer(repo)
  await writer.broadcast(_payload())
  assert repo.logs == []


# ── Dispatcher: CDC plumbing ────────────────────────────────────────


async def test_a_change_notification_dispatches_that_cycle():
  writer, repo = _writer()
  await writer.broadcast(_payload())
  cycle = _cycle_of(repo)

  dispatcher, channel = _dispatcher(repo)
  await dispatcher._on_change({"broadcast_message_id": str(cycle.id), "seq": 1})

  assert len(channel.sent) == 1


async def test_an_unusable_notification_payload_is_ignored():
  writer, repo = _writer()
  await writer.broadcast(_payload())
  dispatcher, channel = _dispatcher(repo)

  await dispatcher._on_change({"broadcast_message_id": "not-a-uuid"})
  await dispatcher._on_change({})

  assert channel.sent == []


async def test_the_sweeper_delivers_what_no_notification_reached():
  """``NOTIFY`` is fire-and-forget: anything appended while the broker was down
  is only ever found by re-reading the log."""
  writer, repo = _writer()
  await writer.broadcast(_payload())

  dispatcher, channel = _dispatcher(repo)
  swept = await dispatcher.sweep()

  assert swept == 1
  assert len(channel.sent) == 1


async def test_start_and_stop_drive_the_listener():
  _, repo = _writer()
  listener = FakeListener()
  dispatcher = BroadcastDispatcher(
    repository=repo,
    setting_repository=FakeSettingRepository(),
    notifier=FakeTelegram(),
    listener=listener,
    private_chat_ids=["-100"],
  )
  await dispatcher.start()
  assert listener.started is True
  await dispatcher.stop()
  assert listener.stopped is True


async def test_per_cycle_locks_do_not_accumulate():
  repo, _, cycle = await _run(_payload())
  dispatcher, _ = _dispatcher(repo)
  await dispatcher.dispatch(cycle.id)
  assert dispatcher._locks == {}


# ── Dispatcher: broker settings ─────────────────────────────────────


async def test_silent_signal_records_the_cycle_but_sends_nothing():
  repo, channel, _ = await _run(_payload(), settings_values={SILENT_SIGNAL: "1"})

  assert channel.sent == []
  # The cycle is still tracked, so the next visible signal shows full history.
  assert _cycle_of(repo).events


async def test_public_chats_come_from_the_broker_setting():
  _, channel, _ = await _run(_payload(), private=(), public="-300,-400")
  assert [chat_id for chat_id, _ in channel.sent] == ["-300", "-400"]


async def test_notification_timezone_setting_is_applied():
  _, channel, _ = await _run(
    _payload(), settings_values={NOTIFICATION_TIMEZONE_KEY: "0"}
  )
  assert "2026-01-01 00:00:00 (UTC+0)" in channel.sent[0][1]


async def test_include_signal_raw_reaches_the_private_copy_only():
  """Strategy internals are an operator's business, not a public channel's."""
  writer, repo = _writer()
  await writer.broadcast(
    _payload(indicators=IndicatorsSchema(wt1=1.5), inputs=InputsSchema(bb_len=20))
  )

  dispatcher, channel = _dispatcher(
    repo,
    private=("-100",),
    public="-300",
    settings_values={NOTIFICATION_INCLUDE_SIGNAL_RAW: "1"},
  )
  await dispatcher.dispatch(_cycle_of(repo).id)

  bodies = {chat_id: text for chat_id, text in channel.sent}
  assert "wt1: 1.5" in bodies["-100"]
  assert "wt1: 1.5" not in bodies["-300"]


# ── Event building ──────────────────────────────────────────────────


def test_event_captures_levels_flags_and_attempt():
  event = build_broadcast_event(
    _payload(
      position=PositionSchema(
        action=SignalActionEnum.LONG,
        price=100.0,
        quantity=2.0,
        sl=95.0,
        tp1=110.0,
        tp2=120.0,
        risk_percent=1.5,
        tp1_percent=50.0,
        move_sl_to_be=True,
        is_running=True,
        is_scale_position=False,
      )
    ),
    attempt_number=2,
  )

  assert event["action"] == "LONG"
  assert event["price"] == 100.0
  assert event["risk_percent"] == 1.5
  assert event["tp1_percent"] == 50.0
  assert event["move_sl_to_be"] is True
  assert event["attempt"] == 2
  assert event["timestamp"] == "2026-01-01T00:00:00+00:00"


def test_event_risk_percent_falls_back_to_inputs():
  event = build_broadcast_event(_payload(inputs=InputsSchema(risk_percent=3.0)))
  assert event["risk_percent"] == 3.0


def test_market_of_reads_the_event_then_the_trade():
  assert bs._market_of(_trade_event(), None) == MarketTypeEnum.FOREX
  assert bs._market_of(_trade_event(market="CRYPTO"), None) == MarketTypeEnum.CRYPTO


# ── Cycle status rules ──────────────────────────────────────────────


def test_only_terminal_actions_close_a_cycle():
  running = [SignalActionEnum.LONG, SignalActionEnum.SHORT, SignalActionEnum.TP1]
  closing = [
    SignalActionEnum.TP2,
    SignalActionEnum.SL,
    SignalActionEnum.R_SL,
    SignalActionEnum.FLAT,
  ]
  assert all(status_for_action(a) == BroadcastStatusEnum.RUNNING for a in running)
  assert all(status_for_action(a) == BroadcastStatusEnum.CLOSED for a in closing)


def test_closed_is_terminal():
  assert (
    merge_status(BroadcastStatusEnum.CLOSED, BroadcastStatusEnum.RUNNING)
    == BroadcastStatusEnum.CLOSED
  )
  assert (
    merge_status(BroadcastStatusEnum.RUNNING, BroadcastStatusEnum.CLOSED)
    == BroadcastStatusEnum.CLOSED
  )
