"""
broker/db/repository/signal.py — the persisted inbound signals
(``SqlAlchemySignalRepository``).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select

from broker.db.models import Signal
from broker.db.repository import _session
from broker.logger import get_logger
from broker.schemas.core import SignalStatusEnum
from broker.schemas.webhook_schema import WebhookPayload
from broker.settings import settings

log = get_logger(__name__)


class SqlAlchemySignalRepository:
  """Persists inbound TradingView webhook signals to the ``signals`` table."""

  async def log_signal(
    self,
    payload: WebhookPayload,
    *,
    delivery_key: str | None = None,
    attempts: int | None = None,
  ) -> str | None:
    """
    Persist a received TradingView webhook signal.

    *delivery_key* identifies the JetStream message the payload came from; it
    is unique on the table, so a second insert for the same message fails
    (and returns ``None``) instead of creating a second ``signal_id``.
    *attempts* seeds the row's attempt budget — the service passes its own, so
    the row and the service count from the same number; defaults to
    ``settings.signal.MAX_ATTEMPTS``.

    Returns
    -------
    str | None
        The UUID of the inserted row, or None if the insert failed.
    """
    pos = payload.position
    new_id = uuid.uuid4()
    row = Signal(
      id=new_id,
      strategy=payload.strategy,
      symbol=payload.symbol,
      timeframe=payload.timeframe,
      timestamp=payload.timestamp,
      signal_uxid=payload.signal_uxid,
      action=pos.action,
      price=pos.price or 0.0,
      quantity=pos.quantity or 0.0,
      sl=pos.sl,
      tp1=pos.tp1,
      tp2=pos.tp2,
      is_running=pos.is_running if pos.is_running is not None else False,
      # Mirror parse_signal's precedence: position-level risk_percent wins,
      # then inputs.risk_percent, else 0.0. Keeping these in sync ensures the
      # persisted audit row matches the signal that was actually published.
      risk_percent=pos.risk_percent
      if pos.risk_percent is not None
      else (
        payload.inputs.risk_percent
        if payload.inputs is not None and payload.inputs.risk_percent is not None
        else 0.0
      ),
      is_scale_position=bool(pos.is_scale_position),
      scale_strategy=pos.scale_strategy,
      status=SignalStatusEnum.QUEUED,
      attempts=attempts if attempts is not None else settings.signal.MAX_ATTEMPTS,
      last_attempt=None,
      delivery_key=delivery_key,
      indicators=json.loads(payload.indicators.model_dump_json())
      if payload.indicators is not None
      else {},
      inputs=json.loads(payload.inputs.model_dump_json())
      if payload.inputs is not None
      else {},
      raw=json.loads(payload.model_dump_json()),
    )
    try:
      async with _session.get_session() as session:
        session.add(row)
      log.debug("signals written for symbol=%s id=%s", payload.symbol, str(new_id))
      return str(new_id)
    except Exception as exc:
      log.exception("Failed to write signals: %s", exc)
      return None

  async def mark_published(self, signal_id: str) -> bool:
    """Flip a row from QUEUED to PUBLISHED once the JetStream handler is done.

    Returns ``True`` when the row exists and the update lands, ``False`` if the
    row is missing or the update fails. Missing is treated as ``False`` — the
    caller can log it, but this is best-effort audit metadata that never blocks
    the acknowledgment on JetStream.
    """
    try:
      row_id = uuid.UUID(signal_id)
    except (TypeError, ValueError):
      log.error("mark_published: invalid signal_id=%r", signal_id)
      return False

    try:
      async with _session.get_session() as session:
        result = await session.execute(select(Signal).where(Signal.id == row_id))
        row: Optional[Signal] = result.scalars().first()
        if row is None:
          log.warning("mark_published: signal_id=%s not found", signal_id)
          return False
        row.status = SignalStatusEnum.PUBLISHED
      return True
    except Exception as exc:
      log.exception("Failed to mark signal published id=%s: %s", signal_id, exc)
      return False

  async def get_by_delivery_key(self, delivery_key: str) -> Signal | None:
    """Return the row persisted from JetStream message *delivery_key*.

    Raises on a database error instead of answering ``None``: the caller is
    deciding whether a redelivered message was already fanned out, and
    "unknown" must not be read as "not yet" — that is how a redelivery turns
    into a second position.
    """
    async with _session.get_session() as session:
      result = await session.execute(
        select(Signal).where(Signal.delivery_key == delivery_key)
      )
      return result.scalars().first()

  async def mark_failed(self, signal_id: str) -> bool:
    """Flip a row straight to ``FAILED`` with no attempts left.

    For failures that no retry can fix — a payload that cannot be turned into
    a ``TradingSignal`` fails the same way every time — so the retry job stops
    re-picking the row immediately instead of burning its remaining attempts.
    """
    try:
      row_id = uuid.UUID(signal_id)
    except (TypeError, ValueError):
      log.error("mark_failed: invalid signal_id=%r", signal_id)
      return False

    try:
      async with _session.get_session() as session:
        result = await session.execute(select(Signal).where(Signal.id == row_id))
        row: Optional[Signal] = result.scalars().first()
        if row is None:
          log.warning("mark_failed: signal_id=%s not found", signal_id)
          return False
        row.status = SignalStatusEnum.FAILED
        row.attempts = 0
        row.last_attempt = datetime.now(timezone.utc)
      return True
    except Exception as exc:
      log.exception("Failed to mark signal failed id=%s: %s", signal_id, exc)
      return False

  async def get_by_id(self, signal_id: str) -> Signal | None:
    """Return the persisted row for *signal_id*, or ``None`` when missing.

    Used by the retry job to rebuild a ``WebhookPayload`` from ``row.raw``
    before re-running the fan-out.
    """
    try:
      row_id = uuid.UUID(signal_id)
    except (TypeError, ValueError):
      log.error("get_by_id: invalid signal_id=%r", signal_id)
      return None

    try:
      async with _session.get_session() as session:
        result = await session.execute(select(Signal).where(Signal.id == row_id))
        return result.scalars().first()
    except Exception as exc:
      log.exception("Failed to fetch signal id=%s: %s", signal_id, exc)
      return None

  async def record_attempt_failure(self, signal_id: str) -> Signal | None:
    """Consume one attempt on a failed fan-out.

    Decrements ``attempts`` and stamps ``last_attempt`` on the row. When the
    row was already at ``attempts == 1`` this call flips it to ``FAILED`` and
    zeroes the counter — the retry job's ``attempts > 0`` filter then stops
    re-picking it. Returns the updated row (or ``None`` if missing / invalid
    id) so the caller can log the transition.
    """
    try:
      row_id = uuid.UUID(signal_id)
    except (TypeError, ValueError):
      log.error("record_attempt_failure: invalid signal_id=%r", signal_id)
      return None

    try:
      async with _session.get_session() as session:
        result = await session.execute(select(Signal).where(Signal.id == row_id))
        row: Optional[Signal] = result.scalars().first()
        if row is None:
          log.warning("record_attempt_failure: signal_id=%s not found", signal_id)
          return None
        row.last_attempt = datetime.now(timezone.utc)
        if row.attempts <= 1:
          row.attempts = 0
          row.status = SignalStatusEnum.FAILED
        else:
          row.attempts -= 1
        await session.flush()
        await session.refresh(row)
      log.info(
        "signal attempt failure id=%s attempts=%d status=%s",
        signal_id,
        row.attempts,
        row.status,
      )
      return row
    except Exception as exc:
      log.exception("Failed to record attempt failure id=%s: %s", signal_id, exc)
      return None

  async def list_retryable(self, retry_interval_seconds: int) -> list[Signal]:
    """Return ``QUEUED`` signals eligible for another attempt, oldest first.

    A row is eligible when it is still ``QUEUED``, has attempts remaining, and
    recorded a failed attempt (``last_attempt``) older than
    ``retry_interval_seconds`` — the same interval the retry job polls at, so a
    row that just failed is not re-picked before the next tick.

    Rows with ``last_attempt IS NULL`` are deliberately *excluded*. A NULL
    ``last_attempt`` never means "ready to retry": it is a signal whose *first*
    fan-out is still in flight (or was interrupted). Only ``record_attempt_failure``
    ever stamps ``last_attempt``; the happy path flips the row straight to
    ``PUBLISHED``. Selecting NULL rows let this job race the in-flight first
    attempt and fan the signal out a second time — re-sending the Telegram
    notification, which is the "one TradingView alert, two Telegram messages"
    duplicate. A first attempt that genuinely crashed before recording anything
    is replayed by JetStream redelivery (the envelope was never acked), not by
    this job, so nothing is dropped by excluding NULL here.

    ``is_not(None)`` is redundant with the ``< threshold`` comparison (SQL
    ``NULL < ts`` is already false) but kept explicit so the intent survives
    future edits.
    """
    threshold = datetime.now(timezone.utc) - timedelta(seconds=retry_interval_seconds)
    try:
      async with _session.get_session() as session:
        result = await session.execute(
          select(Signal)
          .where(Signal.status == SignalStatusEnum.QUEUED)
          .where(Signal.attempts > 0)
          .where(Signal.last_attempt.is_not(None))
          .where(Signal.last_attempt < threshold)
          .order_by(Signal.createdAt.asc())
        )
        return list(result.scalars().all())
    except Exception as exc:
      log.exception("Failed to list retryable signals: %s", exc)
      return []

  async def list_recent_by_strategies(
    self, strategies: list[str], since_seconds: int
  ) -> list[dict]:
    """Return raw webhook payloads for recent signals matching *strategies*.

    Backs the ``retry_signals`` replay a worker gets on connect: rows
    whose ``strategy`` is in *strategies* and whose ``createdAt`` is within the
    last *since_seconds*, newest first. Only the persisted ``raw`` JSON is
    returned so callers can feed it straight through ``parse_signal`` — the
    same code path the JetStream handler uses to produce the SIGNAL payload.
    """
    if not strategies or since_seconds <= 0:
      return []

    since_dt = datetime.now(timezone.utc) - timedelta(seconds=since_seconds)
    try:
      async with _session.get_session() as session:
        result = await session.execute(
          select(Signal)
          .where(Signal.strategy.in_(strategies))
          .where(Signal.createdAt >= since_dt)
          .order_by(Signal.createdAt.asc())
        )
        rows = list(result.scalars().all())
    except Exception as exc:
      log.exception("Failed to list recent signals: %s", exc)
      return []

    envelopes: list[dict] = []
    for row in rows:
      raw = row.raw
      if not raw:
        continue
      envelopes.append(
        {
          "signal_id": str(row.id),
          "payload": raw,
        }
      )
    return envelopes
