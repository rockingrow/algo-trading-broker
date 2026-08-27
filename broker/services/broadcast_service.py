"""
broker/services/broadcast_service.py — One Telegram message per signal *cycle*.

The old behaviour was one message per signal: an entry, its TP1, its SL and a
FLAT produced four unrelated messages in the channel, and a reader had to
stitch the trade back together by eye. A trade now owns a single message per
chat that is **edited in place** as it progresses — including a live table of
which workers executed it and where each of them stands.

The module has two halves, and they never call each other directly:

* :class:`SignalBroadcastService` — the **writer**. A signal (or a worker's
  TRADE report) updates the cycle in Postgres and appends a row to the
  ``broadcast_message_logs`` write log, in one transaction. That is all it
  does: no Telegram call happens on the path of a signal, so a Telegram outage
  or a slow edit can never delay — or lose — a trade.
* :class:`BroadcastDispatcher` — the **reader**. A Postgres trigger fires
  ``pg_notify`` on every write-log insert; the dispatcher listens for it,
  re-renders the whole cycle from its current state and edits each chat's
  message. A sweeper re-reads the log on a timer so anything appended while
  the process was down (``NOTIFY`` is fire-and-forget) is still delivered.
  Because an edit is silent — Telegram notifies nobody when a message is
  rewritten — each new event of the cycle *also* gets a two-line reply under
  that same message, so a reader who saw the trade open finds out that it hit
  TP1 or closed. ``broadcast_message_chats.notified_event_count`` records how
  many events a chat has been told about, so a redelivery never repeats one.
  Either audience can turn this reply off (``private_broadcast_reply_notify`` /
  ``public_broadcast_reply_notify``, both enabled by default) — the message
  keeps being edited in place either way, only the reply notice is silenced.

What ties a cycle together is the pair ``strategy`` + ``signal_uxid`` (see
``WebhookPayload``): the unique key of a ``broadcast_messages`` row. Ordering
and lost-update safety come from the write log — ``last_seq`` is handed out
under a row lock on the cycle, and each chat records the highest sequence it
has been shown (``delivered_seq``), so a slow delivery cannot overwrite a newer
body with an older one.

Two audiences run the same flow off the same cycle row: ``PRIVATE``
(``TELEGRAM_PRIVATE_BROADCAST_CHAT_IDS`` env var) and ``PUBLIC`` (the
``public_broadcast_chat_ids`` broker setting, editable from the admin API and
the bot). ``PUBLIC`` gets the bare price/level/timeline body; ``PRIVATE``
additionally carries the strategy name, signal id, the worker/status table and
(when enabled) the raw indicator/input dump — each chat stores the exact text
it holds, so the two can diverge further without a schema change.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Optional

from broker.constants import (
  NOTIFICATION_INCLUDE_SIGNAL_RAW,
  NOTIFICATION_TIMEZONE_KEY,
  PRIVATE_REPLY_NOTIFY_KEY,
  PUBLIC_BROADCAST_CHAT_IDS_KEY,
  PUBLIC_REPLY_NOTIFY_KEY,
  SILENT_SIGNAL,
)
from broker.db.listener import BROADCAST_LOG_CHANNEL, PostgresChangeListener
from broker.db.models import Trade
from broker.domain.trade_status import TradeStatusPolicy
from broker.helpers.message_formatter import (
  format_broadcast_message,
  format_broadcast_update_notice,
)
from broker.interfaces import (
  BroadcastCycleView,
  BroadcastMessageRepository,
  SettingRepository,
  SignalRepository,
)
from broker.logger import get_logger
from broker.schemas.account_schema import MarketTypeEnum, compose_worker_id
from broker.schemas.core import BroadcastAudienceEnum
from broker.schemas.trade_event_schema import PositionEvent
from broker.schemas.webhook_schema import WebhookPayload
from broker.services.notification_service import (
  BroadcastNotifier,
  ChatTarget,
  EditOutcome,
  parse_chat_targets,
)
from broker.settings import settings

log = get_logger(__name__)


def _parse(raw: str | list[str] | None) -> list[tuple[str, ChatTarget]]:
  """(raw entry, parsed ChatTarget) pairs for one chat-id setting.

  The raw entry is what gets stored on the ``broadcast_message_chats`` row so
  two topics of the same group get distinct rows (``-100…_924584`` vs
  ``-100…_924585``); the ChatTarget is what the notifier sends with.

  Accepts the setting in either shape it turns up in: the plain
  comma-separated string that env vars and the ``public_broadcast_chat_ids``
  broker setting carry, or the pre-split list of entries the tests (and the
  dispatcher's ``private_chat_ids`` override) pass in.
  """
  if not raw:
    return []
  entries = raw.split(",") if isinstance(raw, str) else list(raw)
  pairs: list[tuple[str, ChatTarget]] = []
  seen: set[str] = set()
  for entry in entries:
    entry = entry.strip()
    if not entry or entry in seen:
      continue
    parsed = parse_chat_targets(entry)
    if not parsed:
      continue
    seen.add(entry)
    pairs.append((entry, parsed[0]))
  return pairs


def _raw_id(chat: ChatTarget) -> str:
  """The setting entry that produced *chat* — the persistent key for a chat row."""
  if chat.message_thread_id is None:
    return chat.chat_id
  return f"{chat.chat_id}_{chat.message_thread_id}"


def build_broadcast_event(
  payload: WebhookPayload, *, attempt_number: int | None = None
) -> dict:
  """The JSONB entry appended to a cycle for one signal.

  Everything the message may need to render is captured here, because the body
  is rebuilt from these events alone on every later edit — going back to the
  ``signals`` table for it would make rendering depend on rows this service
  does not own.
  """
  pos = payload.position
  risk_percent = pos.risk_percent
  if risk_percent is None and payload.inputs is not None:
    risk_percent = payload.inputs.risk_percent
  return {
    "action": pos.action.value,
    "price": pos.price,
    "quantity": pos.quantity,
    "sl": pos.sl,
    "tp1": pos.tp1,
    "tp2": pos.tp2,
    "risk_percent": risk_percent,
    "tp1_percent": pos.tp1_percent,
    "move_sl_to_be": pos.move_sl_to_be,
    "use_equity_sizing": pos.use_equity_sizing,
    "is_running": pos.is_running,
    "is_scale_position": pos.is_scale_position,
    "scale_strategy": pos.scale_strategy,
    "timestamp": payload.timestamp.isoformat(),
    "attempt": attempt_number,
    "indicators": payload.indicators.model_dump() if payload.indicators else None,
    "inputs": payload.inputs.model_dump() if payload.inputs else None,
  }


class SignalBroadcastService:
  """Writer half: records what changed on a cycle. Never calls Telegram.

  Both entry points commit the change together with its write-log entry, which
  is what makes delivery recoverable — see the module docstring.
  """

  def __init__(
    self,
    *,
    repository: BroadcastMessageRepository,
    signal_repository: SignalRepository | None = None,
    policy: TradeStatusPolicy | None = None,
  ) -> None:
    self._repository = repository
    # Only needed by the worker path: a TRADE event identifies its signal by
    # the broker's ``signal_id``, and the cycle key lives on that signal row.
    self._signals = signal_repository
    self._policy = policy or TradeStatusPolicy()

  # ── Signal path ────────────────────────────────────────────────────

  async def broadcast(
    self, payload: WebhookPayload, *, attempt_number: int | None = None
  ) -> None:
    """Record *payload* on its cycle. Delivery is the dispatcher's job."""
    if not settings.telegram.ENABLED:
      return

    record = await self._repository.record_event(
      strategy=payload.strategy,
      signal_uxid=payload.signal_uxid,
      symbol=payload.symbol,
      timeframe=payload.timeframe,
      action=payload.position.action,
      event=build_broadcast_event(payload, attempt_number=attempt_number),
    )
    if record is None:
      log.error(
        "Broadcast not recorded strategy=%s signal_uxid=%s",
        payload.strategy,
        payload.signal_uxid,
      )

  # ── Worker path ────────────────────────────────────────────────────

  async def record_execution(
    self, event: PositionEvent, trade: Trade | None = None
  ) -> None:
    """Record that a worker acted on the signal behind *event*.

    Called for every TRADE event. A worker echoes back the ``signal_id`` it was
    given — the ``signals`` row id, unique per signal — so the cycle is found by
    reading that row's ``signal_uxid``. (The SIGNAL payload also carries the
    cycle id directly, but ``PositionEvent`` has no field for it, so this stays
    the one link.) An event without a ``signal_id`` (a manual trade, a worker
    too old to echo it) has no cycle to update and is ignored.
    """
    if not settings.telegram.ENABLED or self._signals is None:
      return
    if not event.signal_id:
      return

    latest_status = self._policy.to_trade_status(event.status)
    if latest_status is None:
      log.debug("Broadcast worker: unknown position status=%s", event.status)
      return

    signal = await self._signals.get_by_id(event.signal_id)
    if signal is None or not signal.signal_uxid:
      return

    market = _market_of(event, trade)
    gateway = event.gateway or (trade.gateway if trade is not None else None)
    await self._repository.record_worker_execution(
      strategy=signal.strategy,
      signal_uxid=signal.signal_uxid,
      worker_id=compose_worker_id(
        market.value if market is not None else "", gateway or "", event.account_id
      ),
      account_id=event.account_id,
      market=market,
      gateway=gateway,
      latest_status=latest_status,
      latest_action=event.status,
      reject_reason=event.reject_reason,
    )


def _market_of(event: PositionEvent, trade: Trade | None) -> MarketTypeEnum | None:
  """The account's market as an enum, from the event or the persisted trade."""
  raw = event.market or (trade.market if trade is not None else None)
  if raw is None:
    return None
  if isinstance(raw, MarketTypeEnum):
    return raw
  try:
    return MarketTypeEnum(raw)
  except ValueError:
    return None


class BroadcastDispatcher:
  """Reader half: turns committed cycle changes into Telegram edits.

  Woken by ``pg_notify`` (see :mod:`broker.db.listener`) and, as a safety net,
  by its own sweeper. Delivery for one cycle is serialised by a per-cycle lock,
  and every send carries the sequence it renders so an out-of-order delivery is
  dropped rather than shown.
  """

  def __init__(
    self,
    *,
    repository: BroadcastMessageRepository,
    setting_repository: SettingRepository,
    notifier: BroadcastNotifier | None = None,
    listener: PostgresChangeListener | None = None,
    private_chat_ids: list[str] | None = None,
    max_attempts: int = 5,
    sweep_interval_seconds: float = 30.0,
    stale_after_seconds: int = 60,
  ) -> None:
    self._repository = repository
    self._settings = setting_repository
    self._notifier = notifier or BroadcastNotifier()
    self._listener = listener or PostgresChangeListener(
      BROADCAST_LOG_CHANNEL, self._on_change
    )
    # Override exists for tests; production reads the env on every dispatch so
    # the targets follow the process environment rather than a snapshot.
    self._private_chat_ids = private_chat_ids
    self._max_attempts = max_attempts
    self._sweep_interval = sweep_interval_seconds
    self._stale_after = stale_after_seconds
    self._locks: dict[str, asyncio.Lock] = {}
    self._sweeper: Optional[asyncio.Task] = None
    self._stop = asyncio.Event()

  # ── lifecycle ──────────────────────────────────────────────────────

  async def start(self) -> None:
    """Subscribe to the change channel and start the sweeper.

    The first sweep runs immediately: anything appended while the broker was
    down never produced a notification this process could hear.
    """
    self._stop.clear()
    await self._listener.start()
    await self.sweep()
    self._sweeper = asyncio.create_task(self._sweep_loop(), name="broadcast-sweeper")
    log.info("Broadcast dispatcher started channel=%s", BROADCAST_LOG_CHANNEL)

  async def stop(self) -> None:
    self._stop.set()
    if self._sweeper is not None:
      self._sweeper.cancel()
      try:
        await self._sweeper
      except asyncio.CancelledError:
        pass
      self._sweeper = None
    await self._listener.stop()
    log.info("Broadcast dispatcher stopped.")

  # ── triggers ───────────────────────────────────────────────────────

  async def _on_change(self, payload: dict) -> None:
    """Handle one ``pg_notify`` payload from the write-log trigger."""
    raw_id = payload.get("broadcast_message_id")
    try:
      cycle_id = uuid.UUID(str(raw_id))
    except (TypeError, ValueError):
      log.warning("Broadcast change with unusable id: %r", raw_id)
      return
    await self.dispatch(cycle_id)

  async def _sweep_loop(self) -> None:
    while not self._stop.is_set():
      try:
        await asyncio.wait_for(self._stop.wait(), timeout=self._sweep_interval)
        return
      except asyncio.TimeoutError:
        pass
      try:
        await self.sweep()
      except asyncio.CancelledError:
        raise
      except Exception as exc:
        log.exception("Broadcast sweep failed: %s", exc)

  async def sweep(self) -> int:
    """Deliver every cycle with outstanding write-log entries.

    Covers the two things a notification cannot: entries written while nothing
    was listening, and entries left in ``SENDING`` by a dispatcher that died
    mid-delivery.
    """
    reclaimed = await self._repository.reclaim_stale_logs(
      stale_after_seconds=self._stale_after
    )
    if reclaimed:
      log.warning("Reclaimed %d stale broadcast log entries", reclaimed)

    cycle_ids = await self._repository.list_cycles_with_pending_logs(
      max_attempts=self._max_attempts, stale_after_seconds=self._stale_after
    )
    for cycle_id in cycle_ids:
      await self.dispatch(cycle_id)
    return len(cycle_ids)

  # ── delivery ───────────────────────────────────────────────────────

  async def dispatch(self, cycle_id: uuid.UUID) -> None:
    """Claim a cycle's pending changes and push the cycle to every chat."""
    lock = self._lock_for(cycle_id)
    try:
      async with lock:
        await self._dispatch_locked(cycle_id)
    finally:
      # Drop the lock once nothing holds or awaits it, so a long-running broker
      # does not accumulate one lock per trade it ever broadcast. A waiter that
      # already has the object keeps it alive (``locked()`` is True), and a
      # waiter that has not acquired yet cannot be interleaved here — there is
      # no await between taking the object and acquiring it.
      if not lock.locked():
        self._locks.pop(str(cycle_id), None)

  async def _dispatch_locked(self, cycle_id: uuid.UUID) -> None:
    """Everything after the per-cycle lock is taken."""
    claimed = await self._repository.claim_pending_logs(
      cycle_id, max_attempts=self._max_attempts
    )
    if not claimed:
      return
    log_ids = [entry.id for entry in claimed]

    view = await self._repository.load_cycle(cycle_id)
    if view is None:
      # The cycle is gone (deleted, or a payload pointing at nothing). There
      # is nothing left to render, so retrying forever would be pointless.
      await self._repository.finish_logs(log_ids, delivered=True)
      return

    settings_values = await self._settings.get_many(
      [
        SILENT_SIGNAL,
        NOTIFICATION_TIMEZONE_KEY,
        NOTIFICATION_INCLUDE_SIGNAL_RAW,
        PUBLIC_BROADCAST_CHAT_IDS_KEY,
        PRIVATE_REPLY_NOTIFY_KEY,
        PUBLIC_REPLY_NOTIFY_KEY,
      ]
    )
    if settings_values.get(SILENT_SIGNAL) == "1":
      # Silenced: the cycle stays recorded, so the next visible change shows
      # the full history. Nothing is left pending — a body that was never
      # sent is not owed to anyone.
      log.debug("SILENT_SIGNAL is enabled; skipping broadcast delivery.")
      await self._repository.finish_logs(log_ids, delivered=True)
      return

    targets = self._targets(settings_values.get(PUBLIC_BROADCAST_CHAT_IDS_KEY))
    if not targets:
      log.debug("No broadcast chats configured; nothing to deliver.")
      await self._repository.finish_logs(log_ids, delivered=True)
      return

    ok = await self._deliver_all(view, targets, settings_values)
    await self._repository.finish_logs(
      log_ids,
      delivered=ok,
      error=None if ok else "delivery failed",
      max_attempts=self._max_attempts,
    )
    await self._repository.mark_broadcast(cycle_id)

  def _targets(
    self, public_raw: str | None
  ) -> list[tuple[BroadcastAudienceEnum, ChatTarget]]:
    """(audience, chat) pairs to broadcast into, private first.

    Private comes from ``TELEGRAM_PRIVATE_BROADCAST_CHAT_IDS`` (a deployment concern),
    public from the ``public_broadcast_chat_ids`` broker setting (edited at
    runtime). Both settings share the same shape — the comma-separated,
    topic-suffixed format ``parse_chat_targets`` understands — so a group with
    Topics enabled can address one specific topic (``-100…_924584``) in either
    audience.

    A chat listed under both audiences is kept once, under the audience that
    claimed it first: one chat holds a single message per cycle. ``chat_id``
    values on the ``broadcast_message_chats`` rows are the *raw* setting
    entries (topic suffix included), so unique-per-(cycle, chat_id) means
    unique per addressable destination — different topics of the same group
    stay distinct.
    """
    private_raw = (
      self._private_chat_ids
      if self._private_chat_ids is not None
      else settings.telegram.PRIVATE_BROADCAST_CHAT_IDS
    )
    private = _parse(private_raw)
    public = _parse(public_raw)

    targets: dict[str, tuple[BroadcastAudienceEnum, ChatTarget]] = {}
    for audience, chats in (
      (BroadcastAudienceEnum.PRIVATE, private),
      (BroadcastAudienceEnum.PUBLIC, public),
    ):
      for raw, chat in chats:
        targets.setdefault(raw, (audience, chat))
    return [(audience, chat) for audience, chat in targets.values()]

  async def _deliver_all(
    self,
    view: BroadcastCycleView,
    targets: list[tuple[BroadcastAudienceEnum, ChatTarget]],
    settings_values: dict[str, str],
  ) -> bool:
    """Render per audience and push to every chat. True when all chats are up
    to date (including chats that were already at this sequence)."""
    timezone_offset = settings_values.get(NOTIFICATION_TIMEZONE_KEY)
    include_raw = settings_values.get(NOTIFICATION_INCLUDE_SIGNAL_RAW) == "1"
    seq = view.message.last_seq or 0

    bodies = {
      # Private is the operator-facing copy: strategy name, signal id, the
      # worker/status table, and (when the setting asks for it) the raw
      # indicator/input dump.
      BroadcastAudienceEnum.PRIVATE: format_broadcast_message(
        view.message,
        workers=view.workers,
        timezone_offset=timezone_offset,
        include_raw=include_raw,
        include_meta=True,
      ),
      # Public gets the bare price/level/timeline body — no strategy internals,
      # no worker execution table.
      BroadcastAudienceEnum.PUBLIC: format_broadcast_message(
        view.message, timezone_offset=timezone_offset
      ),
    }
    chats = {chat.chat_id: chat for chat in view.chats}

    # One notice per event of the cycle, same for both audiences: an edit is
    # silent, so every event a chat has not been told about yet also gets a
    # two-line reply under that chat's message (see ``_notify_updates``) —
    # unless that audience has turned reply notices off.
    events = [event for event in (view.message.events or []) if isinstance(event, dict)]
    notices = [
      format_broadcast_update_notice(view.message, index)
      for index in range(len(events))
    ]
    reply_enabled = {
      BroadcastAudienceEnum.PRIVATE: settings_values.get(PRIVATE_REPLY_NOTIFY_KEY) != "0",
      BroadcastAudienceEnum.PUBLIC: settings_values.get(PUBLIC_REPLY_NOTIFY_KEY) != "0",
    }

    results = []
    for audience, chat in targets:
      results.append(
        await self._deliver(
          record_id=view.message.id,
          audience=audience,
          chat=chat,
          text=bodies[audience],
          seq=seq,
          notices=notices,
          existing=chats.get(_raw_id(chat)),
          notify_enabled=reply_enabled[audience],
        )
      )
    return all(results)

  async def _deliver(
    self,
    *,
    record_id: uuid.UUID,
    audience: BroadcastAudienceEnum,
    chat: ChatTarget,
    text: str,
    seq: int,
    notices: list[str | None],
    existing,
    notify_enabled: bool = True,
  ) -> bool:
    """Edit this chat's message, or send it a new one when there is none.

    Only an edit that reports the message as *gone* (deleted from the channel)
    falls back to a fresh send — a cycle that can no longer be updated is worth
    one new message rather than going silent for the rest of the trade. A
    transient failure keeps the stored id and retries on the next pass instead,
    which is what stops a rate limit from littering the chat with duplicate
    copies of the cycle.

    An edit that lands also posts a reply notice for every event this chat has
    not been told about yet: the edit alone changes the message silently, so a
    reader who saw the trade open would otherwise never learn that it hit TP1
    or closed. ``notify_enabled`` is this audience's reply-notify setting —
    when it is off, notices are still tracked as delivered (see
    ``_notify_updates``) but nothing is sent.
    """
    if existing is not None and (existing.delivered_seq or 0) >= seq:
      # Another dispatcher (or an earlier pass) already showed this chat a body
      # at least this new. Sending again would risk replacing it with an older
      # render.
      return True

    stored_chat_id = _raw_id(chat)
    message_id = getattr(existing, "message_id", None)

    if message_id:
      outcome = await self._notifier.edit_message(chat, message_id, text)
      if outcome is EditOutcome.OK:
        notified = await self._notify_updates(
          chat=chat,
          message_id=message_id,
          notices=notices,
          already_notified=getattr(existing, "notified_event_count", None),
          enabled=notify_enabled,
        )
        await self._repository.upsert_chat(
          record_id,
          audience=audience,
          chat_id=stored_chat_id,
          message_id=message_id,
          message=text,
          delivered_seq=seq,
          notified_event_count=notified,
        )
        return True
      if outcome is EditOutcome.FAILED:
        log.warning(
          "Broadcast edit failed chat_id=%s message_id=%s — retrying next pass",
          chat.label,
          message_id,
        )
        # message_id=None leaves the stored id untouched (see upsert_chat), so
        # the retry edits the same message rather than posting a new one.
        await self._repository.upsert_chat(
          record_id,
          audience=audience,
          chat_id=stored_chat_id,
          message_id=None,
          message=None,
          last_error="edit failed",
        )
        return False
      log.warning(
        "Broadcast message gone chat_id=%s message_id=%s — resending",
        chat.label,
        message_id,
      )

    new_message_id = await self._notifier.send_and_get_message_id(chat, text)
    if new_message_id is None:
      await self._repository.upsert_chat(
        record_id,
        audience=audience,
        chat_id=stored_chat_id,
        message_id=None,
        message=None,
        last_error="send failed",
      )
      log.warning("Broadcast send failed chat_id=%s", chat.label)
      return False

    await self._repository.upsert_chat(
      record_id,
      audience=audience,
      chat_id=stored_chat_id,
      message_id=new_message_id,
      message=text,
      delivered_seq=seq,
      # The message that just went out already shows the whole cycle, so
      # nothing about it is owed a notice.
      notified_event_count=len(notices),
    )
    return True

  async def _notify_updates(
    self,
    *,
    chat: ChatTarget,
    message_id: str,
    notices: list[str | None],
    already_notified: int | None,
    enabled: bool = True,
  ) -> int:
    """Reply to *message_id* once per event this chat has not seen announced.

    Returns the new ``notified_event_count`` for the chat — only ever the count
    of notices actually delivered, so a reply that failed is retried on the
    next pass instead of being silently skipped.

    ``already_notified`` is None for a chat row written before notices existed:
    what it announced is unknowable, so it is caught up silently rather than
    replaying the trade's whole history into the channel at once.

    ``enabled`` is this audience's reply-notify setting. When it is off, every
    outstanding notice is treated as caught up without sending anything — so
    re-enabling it later does not dump the trade's backlog into the chat.
    """
    if already_notified is None:
      return len(notices)
    if not enabled:
      return len(notices)

    sent = already_notified
    for index in range(already_notified, len(notices)):
      notice = notices[index]
      if notice is None:
        sent = index + 1
        continue
      if not await self._notifier.reply_message(chat, message_id, notice):
        log.warning(
          "Broadcast notice failed chat_id=%s message_id=%s — retrying next pass",
          chat.label,
          message_id,
        )
        break
      sent = index + 1
    return sent

  # ── internals ──────────────────────────────────────────────────────

  def _lock_for(self, cycle_id: uuid.UUID) -> asyncio.Lock:
    """Per-cycle lock, so two notifications for the same trade are applied one
    after the other instead of racing each other into the same chat."""
    key = str(cycle_id)
    lock = self._locks.get(key)
    if lock is None:
      lock = asyncio.Lock()
      self._locks[key] = lock
    return lock
