"""
broker/db/repository/broadcast_message.py — broadcast cycles, per-chat messages and
their write log
(``SqlAlchemyBroadcastMessageRepository``).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from broker.db.models import (
  BroadcastMessage,
  BroadcastMessageChat,
  BroadcastMessageLog,
  BroadcastMessageWorker,
)
from broker.domain.broadcast_status import merge_status, status_for_action
from broker.db.repository import _session
from broker.logger import get_logger
from broker.schemas.account_schema import MarketTypeEnum
from broker.interfaces.db_protocol import BroadcastCycleView
from broker.schemas.core import (
  BroadcastAudienceEnum,
  BroadcastLogKindEnum,
  BroadcastLogStatusEnum,
  SignalActionEnum,
)
from broker.schemas.trade_schema import TradeStatusEnum

log = get_logger(__name__)


class SqlAlchemyBroadcastMessageRepository:
  """Owns every table behind the one-message-per-signal-cycle Telegram
  broadcast: the cycle itself, its per-chat messages, the workers that executed
  it, and the append-only write log that drives delivery.

  Nothing here talks to Telegram. Writers record a change and append a log row
  in the same transaction; a Postgres trigger notifies, and the dispatcher
  reads the log back out (see ``broker/services/broadcast_service.py``). Two
  rules make that safe under concurrency:

  * every write that touches a cycle takes a ``FOR UPDATE`` row lock on it, so
    two signals arriving together queue instead of both reading the same
    ``events`` list and one overwriting the other's append;
  * the lock also hands out ``last_seq``, the cycle's version, which the
    dispatcher compares against each chat's ``delivered_seq`` so a slow
    delivery can never replace a newer message body with an older one.
  """

  # ── Writers ────────────────────────────────────────────────────────

  async def record_event(
    self,
    *,
    strategy: str,
    signal_uxid: str,
    symbol: str,
    timeframe: str | None,
    action: SignalActionEnum,
    event: dict,
  ) -> BroadcastMessage | None:
    """Find-or-create the cycle for ``(symbol, strategy, signal_uxid)``, append
    *event* to it and log the change. Returns the up-to-date row, or None on
    failure.

    This is one call rather than a get + create/update pair because the unique
    key is exactly what makes the cycle idempotent: two signals of the same
    trade arriving close together (or a JetStream redelivery racing the retry
    job) must converge on one row. A losing insert raises ``IntegrityError``,
    which is retried once — the second pass finds the winner's row and takes
    the update branch.

    Replays are also de-duplicated inside the cycle: an event whose action and
    timestamp match the last recorded one is not appended a second time, and
    logs no change, so a redelivered signal costs nothing instead of doubling a
    line in the message.
    """
    for attempt in (1, 2):
      try:
        async with _session.get_session() as session:
          row = await self._locked_cycle(session, symbol, strategy, signal_uxid)
          incoming_status = status_for_action(action)

          if row is None:
            row = BroadcastMessage(
              id=uuid.uuid4(),
              strategy=strategy,
              signal_uxid=signal_uxid,
              symbol=symbol,
              timeframe=timeframe,
              actions=action.value,
              latest_action=action,
              status=incoming_status,
              events=[event],
              last_seq=1,
            )
            session.add(row)
            await session.flush()
            session.add(
              _log_entry(row, seq=1, kind=BroadcastLogKindEnum.SIGNAL, payload=event)
            )
            await session.flush()
            await session.refresh(row)
            log.info(
              "broadcast cycle opened symbol=%s strategy=%s signal_uxid=%s action=%s",
              symbol,
              strategy,
              signal_uxid,
              action.value,
            )
            return row

          if _is_duplicate_event(row.events, event):
            # A replay: the cycle already shows this line, so there is nothing
            # to deliver. Returning the row keeps the caller's flow uniform.
            log.debug(
              "broadcast cycle replay ignored symbol=%s strategy=%s signal_uxid=%s "
              "action=%s",
              symbol,
              strategy,
              signal_uxid,
              action.value,
            )
            return row

          # Reassign rather than append: SQLAlchemy does not track in-place
          # mutation of a JSONB list, so an ``.append`` would never be flushed.
          row.events = list(row.events or []) + [event]
          row.actions = ",".join(
            [part for part in (row.actions or "").split(",") if part] + [action.value]
          )
          row.latest_action = action
          row.status = merge_status(row.status, incoming_status)
          row.last_seq = (row.last_seq or 0) + 1
          session.add(
            _log_entry(
              row, seq=row.last_seq, kind=BroadcastLogKindEnum.SIGNAL, payload=event
            )
          )
          await session.flush()
          await session.refresh(row)
          log.debug(
            "broadcast cycle updated symbol=%s strategy=%s signal_uxid=%s "
            "action=%s seq=%d",
            symbol,
            strategy,
            signal_uxid,
            action.value,
            row.last_seq,
          )
          return row
      except IntegrityError:
        if attempt == 1:
          log.debug(
            "broadcast cycle insert raced symbol=%s strategy=%s signal_uxid=%s "
            "— retrying",
            symbol,
            strategy,
            signal_uxid,
          )
          continue
        log.exception(
          "Failed to record broadcast event symbol=%s strategy=%s signal_uxid=%s",
          symbol,
          strategy,
          signal_uxid,
        )
        return None
      except Exception as exc:
        log.exception(
          "Failed to record broadcast event symbol=%s strategy=%s signal_uxid=%s: %s",
          symbol,
          strategy,
          signal_uxid,
          exc,
        )
        return None
    return None

  async def record_worker_execution(
    self,
    *,
    symbol: str,
    strategy: str,
    signal_uxid: str,
    worker_id: str,
    account_id: str,
    market: MarketTypeEnum | None,
    gateway: str | None,
    latest_status: TradeStatusEnum,
    latest_action: str | None = None,
    reject_reason: str | None = None,
    event_at: datetime | None = None,
  ) -> BroadcastMessage | None:
    """Record that *worker_id* executed the cycle, and log the change.

    Returns the cycle (so the caller can log it), or None when there is no
    cycle for ``(symbol, strategy, signal_uxid)`` — a worker can report a trade
    for a signal that was never broadcast (Telegram off at the time, an older signal
    predating the cycle tables), and that is not an error.

    A repeat of the status a worker already showed is dropped without touching
    ``last_seq``: TRADE events are chatty, and re-rendering an identical table
    would spend a Telegram edit per event for no visible change.
    """
    try:
      async with _session.get_session() as session:
        row = await self._locked_cycle(session, symbol, strategy, signal_uxid)
        if row is None:
          return None

        result = await session.execute(
          select(BroadcastMessageWorker).where(
            BroadcastMessageWorker.broadcast_message_id == row.id,
            BroadcastMessageWorker.worker_id == worker_id,
          )
        )
        worker: Optional[BroadcastMessageWorker] = result.scalars().first()
        now = event_at or datetime.now(timezone.utc)

        if worker is not None:
          unchanged = (
            worker.latest_status == latest_status
            and worker.latest_action == latest_action
          )
          if unchanged:
            return row
          worker.latest_status = latest_status
          worker.latest_action = latest_action
          worker.reject_reason = reject_reason
          worker.last_event_at = now
          if market is not None:
            worker.market = market
          if gateway is not None:
            worker.gateway = gateway
        else:
          session.add(
            BroadcastMessageWorker(
              id=uuid.uuid4(),
              broadcast_message_id=row.id,
              worker_id=worker_id,
              account_id=account_id,
              market=market,
              gateway=gateway,
              latest_status=latest_status,
              latest_action=latest_action,
              reject_reason=reject_reason,
              last_event_at=now,
            )
          )

        row.last_seq = (row.last_seq or 0) + 1
        session.add(
          _log_entry(
            row,
            seq=row.last_seq,
            kind=BroadcastLogKindEnum.EXECUTION,
            payload={
              "worker_id": worker_id,
              "account_id": account_id,
              "status": getattr(latest_status, "value", str(latest_status)),
              "action": latest_action,
            },
          )
        )
        await session.flush()
        await session.refresh(row)
        log.debug(
          "broadcast worker recorded symbol=%s strategy=%s signal_uxid=%s "
          "worker_id=%s status=%s",
          symbol,
          strategy,
          signal_uxid,
          worker_id,
          latest_status,
        )
        return row
    except Exception as exc:
      log.exception(
        "Failed to record broadcast worker symbol=%s strategy=%s signal_uxid=%s "
        "worker_id=%s: %s",
        symbol,
        strategy,
        signal_uxid,
        worker_id,
        exc,
      )
      return None

  # ── Dispatcher side ────────────────────────────────────────────────

  async def claim_pending_logs(
    self, broadcast_message_id: uuid.UUID, *, max_attempts: int
  ) -> list[BroadcastMessageLog]:
    """Take ownership of a cycle's undelivered write-log entries.

    Flips them ``PENDING`` → ``SENDING`` and bumps their attempt counter inside
    one short transaction (no Telegram call is made while a lock is held), so a
    second dispatcher — or the sweeper running alongside the listener — cannot
    pick up the same entries. Entries past ``max_attempts`` are left alone;
    ``finish_logs`` is what marks them FAILED.
    """
    try:
      async with _session.get_session() as session:
        # Locking the cycle serialises against the writers, so a claim never
        # races an in-flight append of the next seq.
        await session.execute(
          select(BroadcastMessage)
          .where(BroadcastMessage.id == broadcast_message_id)
          .with_for_update()
        )
        result = await session.execute(
          select(BroadcastMessageLog)
          .where(
            BroadcastMessageLog.broadcast_message_id == broadcast_message_id,
            BroadcastMessageLog.status == BroadcastLogStatusEnum.PENDING,
            BroadcastMessageLog.attempts < max_attempts,
          )
          .order_by(BroadcastMessageLog.seq.asc())
          .with_for_update(skip_locked=True)
        )
        rows = list(result.scalars().all())
        for row in rows:
          row.status = BroadcastLogStatusEnum.SENDING
          row.attempts += 1
        await session.flush()
        return rows
    except Exception as exc:
      log.exception(
        "Failed to claim broadcast logs broadcast_message_id=%s: %s",
        broadcast_message_id,
        exc,
      )
      return []

  async def finish_logs(
    self,
    log_ids: list[uuid.UUID],
    *,
    delivered: bool,
    error: str | None = None,
    max_attempts: int | None = None,
  ) -> bool:
    """Close out claimed entries: DELIVERED, or back to PENDING for another
    pass — FAILED once they have used up ``max_attempts``."""
    if not log_ids:
      return True
    try:
      async with _session.get_session() as session:
        result = await session.execute(
          select(BroadcastMessageLog).where(BroadcastMessageLog.id.in_(log_ids))
        )
        for row in result.scalars().all():
          if delivered:
            row.status = BroadcastLogStatusEnum.DELIVERED
            row.delivered_at = datetime.now(timezone.utc)
            row.last_error = None
            continue
          row.last_error = (error or "")[:255] or None
          exhausted = max_attempts is not None and row.attempts >= max_attempts
          row.status = (
            BroadcastLogStatusEnum.FAILED
            if exhausted
            else BroadcastLogStatusEnum.PENDING
          )
      return True
    except Exception as exc:
      log.exception("Failed to finish broadcast logs: %s", exc)
      return False

  async def defer_logs(self, log_ids: list[uuid.UUID], *, reason: str) -> bool:
    """Hand claimed entries back as ``PENDING`` without spending an attempt.

    A deferral is not a failed delivery: nothing was tried, because the
    configuration said not to broadcast right now. The attempt counter is
    rolled back so a cycle parked for a day of silence is not quietly
    exhausted past ``max_attempts`` and dropped — it waits for the setting to
    change, then delivers the edit it owes.
    """
    if not log_ids:
      return True
    try:
      async with _session.get_session() as session:
        result = await session.execute(
          select(BroadcastMessageLog).where(BroadcastMessageLog.id.in_(log_ids))
        )
        for row in result.scalars().all():
          row.status = BroadcastLogStatusEnum.PENDING
          row.attempts = max(0, (row.attempts or 0) - 1)
          row.last_error = (reason or "")[:255] or None
      return True
    except Exception as exc:
      log.exception("Failed to defer broadcast logs: %s", exc)
      return False

  async def list_cycles_with_pending_logs(
    self, *, max_attempts: int, stale_after_seconds: int, limit: int = 50
  ) -> list[uuid.UUID]:
    """Cycles holding work the dispatcher has not delivered.

    This is the safety net under ``pg_notify``: a notification is fire-and-
    forget, so anything appended while the broker was down (or while its
    listener connection was broken) would otherwise sit unseen. Entries stuck
    in ``SENDING`` past *stale_after_seconds* are picked up too — that state
    only survives a dispatcher that died mid-delivery.
    """
    threshold = datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)
    try:
      async with _session.get_session() as session:
        result = await session.execute(
          select(BroadcastMessageLog.broadcast_message_id)
          .where(
            BroadcastMessageLog.attempts < max_attempts,
            or_(
              BroadcastMessageLog.status == BroadcastLogStatusEnum.PENDING,
              and_(
                BroadcastMessageLog.status == BroadcastLogStatusEnum.SENDING,
                BroadcastMessageLog.updatedAt < threshold,
              ),
            ),
          )
          .group_by(BroadcastMessageLog.broadcast_message_id)
          .order_by(func.min(BroadcastMessageLog.seq).asc())
          .limit(limit)
        )
        return list(result.scalars().all())
    except Exception as exc:
      log.exception("Failed to list cycles with pending broadcast logs: %s", exc)
      return []

  async def reclaim_stale_logs(self, *, stale_after_seconds: int) -> int:
    """Return entries stranded in ``SENDING`` to ``PENDING`` so they can be
    claimed again. Called by the sweeper before it dispatches."""
    threshold = datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)
    try:
      async with _session.get_session() as session:
        result = await session.execute(
          select(BroadcastMessageLog).where(
            BroadcastMessageLog.status == BroadcastLogStatusEnum.SENDING,
            BroadcastMessageLog.updatedAt < threshold,
          )
        )
        rows = list(result.scalars().all())
        for row in rows:
          row.status = BroadcastLogStatusEnum.PENDING
        return len(rows)
    except Exception as exc:
      log.exception("Failed to reclaim stale broadcast logs: %s", exc)
      return 0

  async def load_cycle(
    self, broadcast_message_id: uuid.UUID
  ) -> BroadcastCycleView | None:
    """The cycle plus everything needed to render and deliver it.

    Read *after* claiming, so the body always reflects the newest state — even
    newer than the claimed entries. Any change that lands in between simply
    leaves its own entry pending, and the redundant edit it triggers is a no-op
    Telegram reports as "message is not modified".
    """
    try:
      async with _session.get_session() as session:
        result = await session.execute(
          select(BroadcastMessage).where(BroadcastMessage.id == broadcast_message_id)
        )
        message: Optional[BroadcastMessage] = result.scalars().first()
        if message is None:
          return None

        result = await session.execute(
          select(BroadcastMessageChat)
          .where(BroadcastMessageChat.broadcast_message_id == broadcast_message_id)
          .order_by(BroadcastMessageChat.createdAt.asc())
        )
        chats = list(result.scalars().all())

        result = await session.execute(
          select(BroadcastMessageWorker)
          .where(BroadcastMessageWorker.broadcast_message_id == broadcast_message_id)
          .order_by(BroadcastMessageWorker.createdAt.asc())
        )
        workers = list(result.scalars().all())
        return BroadcastCycleView(message=message, chats=chats, workers=workers)
    except Exception as exc:
      log.exception(
        "Failed to load broadcast cycle broadcast_message_id=%s: %s",
        broadcast_message_id,
        exc,
      )
      return None

  async def upsert_chat(
    self,
    broadcast_message_id: uuid.UUID,
    *,
    audience: BroadcastAudienceEnum,
    chat_id: str,
    message_id: str | None,
    message: str | None,
    delivered_seq: int | None = None,
    notified_event_count: int | None = None,
    last_error: str | None = None,
  ) -> bool:
    """Record what a single chat currently holds for this cycle.

    ``message_id`` is only overwritten when a new one is supplied, so a failed
    edit (which passes None) never loses the id needed to edit that message
    again later. ``delivered_seq`` and ``notified_event_count`` only ever move
    forward, which is what makes a late delivery harmless: it cannot pull the
    chat back to an older body, nor make it repeat a reply notice it has
    already posted.
    """
    try:
      async with _session.get_session() as session:
        result = await session.execute(
          select(BroadcastMessageChat).where(
            BroadcastMessageChat.broadcast_message_id == broadcast_message_id,
            BroadcastMessageChat.chat_id == chat_id,
          )
        )
        row: Optional[BroadcastMessageChat] = result.scalars().first()
        if row is None:
          session.add(
            BroadcastMessageChat(
              id=uuid.uuid4(),
              broadcast_message_id=broadcast_message_id,
              audience=audience,
              chat_id=chat_id,
              message_id=message_id,
              message=message,
              delivered_seq=delivered_seq or 0,
              notified_event_count=notified_event_count,
              last_error=last_error,
            )
          )
          return True

        row.audience = audience
        if message_id is not None:
          row.message_id = message_id
        if message is not None:
          row.message = message
        if delivered_seq is not None and delivered_seq > (row.delivered_seq or 0):
          row.delivered_seq = delivered_seq
        if notified_event_count is not None and notified_event_count > (
          row.notified_event_count or 0
        ):
          row.notified_event_count = notified_event_count
        row.last_error = last_error
      return True
    except Exception as exc:
      log.exception(
        "Failed to upsert broadcast chat broadcast_message_id=%s chat_id=%s: %s",
        broadcast_message_id,
        chat_id,
        exc,
      )
      return False

  async def mark_broadcast(self, broadcast_message_id: uuid.UUID) -> bool:
    """Stamp ``last_broadcast_at`` after a cycle has been pushed to Telegram."""
    try:
      async with _session.get_session() as session:
        result = await session.execute(
          select(BroadcastMessage).where(BroadcastMessage.id == broadcast_message_id)
        )
        row: Optional[BroadcastMessage] = result.scalars().first()
        if row is None:
          return False
        row.last_broadcast_at = datetime.now(timezone.utc)
      return True
    except Exception as exc:
      log.exception(
        "Failed to stamp broadcast time broadcast_message_id=%s: %s",
        broadcast_message_id,
        exc,
      )
      return False

  # ── Internals ──────────────────────────────────────────────────────

  async def _locked_cycle(
    self, session: AsyncSession, symbol: str, strategy: str, signal_uxid: str
  ) -> Optional[BroadcastMessage]:
    """The cycle row for the key, locked ``FOR UPDATE`` for the transaction.

    The lock is the whole point: ``events`` is a JSONB list that is read,
    extended and written back, so two concurrent writers without it would each
    append to the same snapshot and the second commit would silently drop the
    first one's line.

    The key is the full ``(symbol, strategy, signal_uxid)`` triple — the same
    columns as the table's unique constraint — so one symbol + strategy can
    carry several live cycles at once and each one locks only its own row.
    """
    result = await session.execute(
      select(BroadcastMessage)
      .where(
        BroadcastMessage.symbol == symbol,
        BroadcastMessage.strategy == strategy,
        BroadcastMessage.signal_uxid == signal_uxid,
      )
      .with_for_update()
    )
    return result.scalars().first()


def _is_duplicate_event(events: list | None, event: dict) -> bool:
  """True when *event* repeats the cycle's last recorded signal.

  Keyed on action + payload timestamp: those two identify one TradingView
  alert, so a redelivered or replayed signal is recognised while a genuine
  second TP1 at a later timestamp still lands.
  """
  if not events:
    return False
  last = events[-1]
  if not isinstance(last, dict):
    return False
  return (last.get("action"), last.get("timestamp")) == (
    event.get("action"),
    event.get("timestamp"),
  )


def _log_entry(
  row: BroadcastMessage, *, seq: int, kind: BroadcastLogKindEnum, payload: dict
) -> BroadcastMessageLog:
  """A write-log entry for a change just made to *row*, in the same transaction.

  Committing the change and its log entry together is what makes delivery
  recoverable: the Telegram edit can fail, the process can die, the
  notification can be missed — the entry is still on disk for the dispatcher
  (or its sweeper) to pick up.
  """
  return BroadcastMessageLog(
    id=uuid.uuid4(),
    broadcast_message_id=row.id,
    seq=seq,
    kind=kind,
    payload=payload,
    status=BroadcastLogStatusEnum.PENDING,
    attempts=0,
  )
