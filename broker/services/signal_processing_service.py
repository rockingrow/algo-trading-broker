"""
broker/services/signal_processing_service.py — webhook pipeline, JetStream
handler pipeline, retry pipeline, and the JetStream consumer that feeds them,
all in one module.

There are three entry points, all built on the same fan-out helper:

* ``enqueue`` — called by the HTTP handler. Verifies the token and pushes the
  raw envelope onto JetStream. Nothing else — no DB write, no block check, no
  publish, no notification. TradingView therefore gets its ``202`` back as
  soon as JetStream ack-s the write, closing the ``server closed the
  connection unexpectedly`` failure mode from holding the request open.

  That ack is waited for under a hard deadline
  (``settings.webhook.ENQUEUE_TIMEOUT``, well inside TradingView's own
  patience), because nats-py otherwise waits 5s for it — long enough for one
  slow ack to fail the delivery as ``request took too long and timed out``.
  An enqueue that overruns the deadline is handed to ``DeferredEnqueuer``
  (below) and the alert still gets its ``202``: the signal keeps being retried
  in the background instead of being lost to a NATS hiccup TradingView will
  never re-deliver.

* ``handle_enqueued`` — called by ``SignalWorker``, the JetStream consumer
  defined below. Runs the block gate, persists the row (``status=QUEUED``,
  ``attempts=SIGNAL_MAX_ATTEMPTS``) and delegates to ``_fanout``. First
  attempt on a signal.

* ``retry_signal`` — called by the retry job. Loads the row (already
  persisted), rebuilds the payload from ``row.raw`` and delegates to
  ``_fanout``. Second / third attempt on the same row.

``_fanout`` publishes on the strategy subject and hands the signal to the
broadcaster, which folds it into the one Telegram message that represents its
signal cycle (see ``broker/services/broadcast_service.py``). On success the row
is flipped to ``PUBLISHED``; on failure the row's ``attempts`` counter is
decremented (and turned into ``FAILED`` when it hits zero) so the retry job
knows whether to pick it up again.

The service depends only on abstractions (``SignalRepository``,
``SettingRepository``, ``SignalPublisher``, ``Notifier``, ``SignalBroadcaster``)
so all three flows can be exercised with in-memory fakes. It raises
``SignalError`` for HTTP translation on the enqueue path only.

``SignalWorker`` pulls envelopes back off JetStream and hands them to
``handle_enqueued`` — the webhook route only persists the signal and enqueues
it, so splitting the pipeline this way keeps the HTTP request short enough
that TradingView's connection is not held open across the fan-out. JetStream's
own redelivery handles crashes: an envelope is only ``ack``-ed after
``handle_enqueued`` succeeds, so a broker restart mid-processing replays the
message.

``DeferredEnqueuer`` is the safety net behind the webhook's deadline: a
background task that keeps re-publishing envelopes JetStream did not accept in
time. Every attempt reuses the envelope's ``Nats-Msg-Id``, so an ack that was
merely slow (the message *did* land) is de-duplicated by the stream rather
than replayed into a second position.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from nats.errors import TimeoutError as NatsTimeoutError
from nats.js.errors import BadRequestError
from pydantic import ValidationError

from broker.constants import SIGNAL_BLOCKED
from broker.helpers.message_formatter import format_blocked_message
from broker.helpers.signal_helper import parse_signal
from broker.interfaces import (
  Notifier,
  SettingRepository,
  SignalBroadcaster,
  SignalPublisher,
  SignalRepository,
)
from broker.logger import get_logger
from broker.nats import (
  JETSTREAM_SIGNAL_STREAM,
  JETSTREAM_SIGNAL_SUBJECT_FILTER,
  NatsClient,
  nats_client,
)
from broker.schemas.core import SignalActionEnum, SignalStatusEnum
from broker.schemas.webhook_schema import WebhookPayload
from broker.settings import settings

log = get_logger(__name__)


# Convenience getter kept out of the class to make it easier to import from
# tests without instantiating the service.
def attempt_number_for_notification(attempts_before: int) -> Optional[int]:
  return _attempt_number_for_notification(attempts_before)


class SignalError(Exception):
  """Framework-agnostic error carrying an HTTP-ish status code and detail."""

  def __init__(self, status_code: int, detail: str) -> None:
    super().__init__(detail)
    self.status_code = status_code
    self.detail = detail


# Envelopes waiting on a JetStream that would not ack in time. Bounded so a
# prolonged outage cannot grow the process' memory without limit; at
# TradingView's alert cadence this is minutes' worth of backlog.
DEFERRED_QUEUE_MAXSIZE = 500


@dataclass
class _DeferredEnvelope:
  """One webhook envelope still owed to JetStream."""

  strategy: str
  envelope: dict
  msg_id: str
  attempts: int = field(default=0)


class DeferredEnqueuer:
  """Retries webhook envelopes JetStream did not accept inside the HTTP deadline.

  The webhook path may only spend ``settings.webhook.ENQUEUE_TIMEOUT`` on the
  PubAck before TradingView loses patience, but a NATS blip lasting a second is
  no reason to lose a trading signal that TradingView will never send again. So
  the route hands the envelope here and answers ``202`` immediately, and this
  task keeps trying — every ``interval`` seconds, up to ``max_attempts`` times —
  on the broker's own time.

  Retries carry the original ``msg_id`` as ``Nats-Msg-Id``. If the first
  publish did reach the stream and only its ack was slow, JetStream's
  duplicate window (``JETSTREAM_DUPLICATE_WINDOW_SECONDS``) drops the retry
  instead of handing workers the same alert twice.

  The backlog lives in memory: a broker killed while envelopes are pending
  loses them (they were never durably stored — that is precisely what failed),
  so giving up on one is logged at ``error`` to reach the operator.
  """

  def __init__(
    self,
    publisher: SignalPublisher,
    *,
    interval_seconds: float | None = None,
    max_attempts: int | None = None,
    maxsize: int = DEFERRED_QUEUE_MAXSIZE,
  ) -> None:
    self._publisher = publisher
    self._interval = (
      interval_seconds
      if interval_seconds is not None
      else settings.webhook.DEFERRED_ENQUEUE_INTERVAL
    )
    self._max_attempts = (
      max_attempts
      if max_attempts is not None
      else settings.webhook.DEFERRED_ENQUEUE_MAX_ATTEMPTS
    )
    self._queue: asyncio.Queue[_DeferredEnvelope] = asyncio.Queue(maxsize=maxsize)
    self._task: Optional[asyncio.Task] = None

  @property
  def pending(self) -> int:
    """Envelopes still owed to JetStream."""
    return self._queue.qsize()

  async def start(self) -> None:
    """Launch the retry loop; safe to call once per app lifetime."""
    if self._task is not None and not self._task.done():
      return
    self._task = asyncio.create_task(self._run(), name="webhook-deferred-enqueue")
    log.info(
      "Deferred webhook enqueue worker started (interval=%.1fs max_attempts=%d)",
      self._interval,
      self._max_attempts,
    )

  async def stop(self) -> None:
    """Cancel the retry loop, reporting anything left unqueued."""
    if self._task is not None:
      self._task.cancel()
      try:
        await self._task
      except asyncio.CancelledError:
        pass
      self._task = None
    if self.pending:
      log.error(
        "Deferred webhook enqueue stopped with %d envelope(s) never queued",
        self.pending,
      )
    log.info("Deferred webhook enqueue worker stopped.")

  def submit(self, *, strategy: str, envelope: dict, msg_id: str) -> bool:
    """Take ownership of *envelope*. Synchronous and non-blocking by design —
    it is called from the HTTP handler, which must not wait on anything.

    Returns False when the backlog is full, i.e. the signal is being dropped
    and the caller should say so rather than answer ``202``.
    """
    item = _DeferredEnvelope(strategy=strategy, envelope=envelope, msg_id=msg_id)
    try:
      self._queue.put_nowait(item)
    except asyncio.QueueFull:
      log.error(
        "Deferred webhook enqueue backlog full (%d) — dropping signal "
        "strategy=%s msg_id=%s",
        self._queue.maxsize,
        strategy,
        msg_id,
      )
      return False
    log.warning(
      "Webhook enqueue deferred strategy=%s msg_id=%s backlog=%d",
      strategy,
      msg_id,
      self.pending,
    )
    return True

  async def _run(self) -> None:
    while True:
      item = await self._queue.get()
      try:
        await self._attempt(item)
      except asyncio.CancelledError:
        raise
      except Exception as exc:
        # A bad item must never kill the loop — the next signal still needs it.
        log.exception("Deferred enqueue attempt crashed: %s", exc)
      finally:
        self._queue.task_done()

  async def _attempt(self, item: _DeferredEnvelope) -> None:
    """One publish attempt; re-queues the envelope when it fails again."""
    item.attempts += 1
    try:
      await self._publisher.publish_webhook_event(
        signal_id="",
        strategy=item.strategy,
        envelope=item.envelope,
        msg_id=item.msg_id,
      )
    except Exception as exc:
      if item.attempts >= self._max_attempts:
        log.error(
          "Deferred enqueue gave up after %d attempts — signal lost "
          "strategy=%s msg_id=%s: %s",
          item.attempts,
          item.strategy,
          item.msg_id,
          exc,
        )
        return
      log.warning(
        "Deferred enqueue attempt %d/%d failed strategy=%s msg_id=%s: %s",
        item.attempts,
        self._max_attempts,
        item.strategy,
        item.msg_id,
        exc,
      )
      # Wait before re-queueing rather than after picking the item up, so the
      # loop can never spin on a backlog that is failing fast (NATS down
      # raises immediately).
      await asyncio.sleep(self._interval)
      try:
        self._queue.put_nowait(item)
      except asyncio.QueueFull:
        log.error(
          "Deferred enqueue backlog full on retry — signal lost strategy=%s msg_id=%s",
          item.strategy,
          item.msg_id,
        )
      return

    log.info(
      "Deferred enqueue succeeded on attempt %d strategy=%s msg_id=%s",
      item.attempts,
      item.strategy,
      item.msg_id,
    )


def _attempt_number_for_notification(attempts_before: int) -> int | None:
  """Sequence number (1-based) of the current attempt for the Telegram line.

  Rendered as ``Attempt: N`` only for the 2nd and 3rd attempt on a signal —
  the first attempt is the default path and doesn't warrant a marker. Returns
  ``None`` when the line should be suppressed.
  """
  if attempts_before > 2:
    return None
  return settings.signal.MAX_ATTEMPTS - attempts_before + 1


class SignalProcessingService:
  """Coordinates persistence, JetStream enqueue, publishing, and notification."""

  def __init__(
    self,
    *,
    signal_repository: SignalRepository,
    setting_repository: SettingRepository,
    publisher: SignalPublisher,
    notifier: Notifier,
    broadcaster: SignalBroadcaster | None = None,
    webhook_secret: str,
    deferred_enqueuer: Optional[DeferredEnqueuer] = None,
  ) -> None:
    self._signals = signal_repository
    self._settings = setting_repository
    self._publisher = publisher
    # Operational channel (broker-log chats): the blocked-signal warning and
    # anything else an operator — not a signal subscriber — needs to see.
    self._notifier = notifier
    # Subscriber-facing channel: one edited-in-place message per signal cycle.
    # None disables signal broadcasts entirely (no chats configured, tests).
    self._broadcaster = broadcaster
    self._webhook_secret = webhook_secret
    self._deferred = deferred_enqueuer

  # ── Enqueue path (called from the webhook route) ───────────────────

  async def enqueue(self, payload: WebhookPayload) -> Dict[str, Any]:
    """Verify the token and hand the raw envelope to JetStream. Fast path.

    No DB write, no block check, no worker fan-out — everything moved into
    ``handle_enqueued`` so TradingView is not held open across the pipeline.

    The wait for JetStream's ack is capped at
    ``settings.webhook.ENQUEUE_TIMEOUT``; past that the envelope goes to the
    ``DeferredEnqueuer`` and the caller is answered anyway, because a webhook
    delivery TradingView times out is never retried and the signal would
    otherwise be gone.
    """
    self._verify_token(payload)
    envelope = {"payload": payload.model_dump(mode="json")}
    # Stable across every retry of *this* alert, so a re-publish of an envelope
    # whose first ack was merely slow is de-duplicated by JetStream instead of
    # reaching workers twice.
    msg_id = uuid.uuid4().hex
    deadline = settings.webhook.ENQUEUE_TIMEOUT

    try:
      # Belt and braces: the inner timeout is nats-py's own bound on the
      # PubAck, the outer one covers everything else the publish could block
      # on (a stalled socket write, a connection being torn down under it).
      await asyncio.wait_for(
        self._publisher.publish_webhook_event(
          signal_id="",
          strategy=payload.strategy,
          envelope=envelope,
          timeout=deadline,
          msg_id=msg_id,
        ),
        timeout=deadline,
      )
    except Exception as exc:
      return self._defer(payload=payload, envelope=envelope, msg_id=msg_id, exc=exc)

    return {
      "status": "queued",
      "timestamp": payload.timestamp.isoformat(),
    }

  def _defer(
    self,
    *,
    payload: WebhookPayload,
    envelope: dict,
    msg_id: str,
    exc: Exception,
  ) -> Dict[str, Any]:
    """Hand a failed/too-slow enqueue to the background retry queue.

    Raises ``SignalError`` only when the signal really is being dropped (no
    queue wired, or its backlog is full) — a fast ``503`` at least shows up in
    TradingView's alert log, where a timeout tells the operator nothing about
    which side gave up.
    """
    log.warning(
      "JetStream enqueue did not complete within %.2fs strategy=%s: %s",
      settings.webhook.ENQUEUE_TIMEOUT,
      payload.strategy,
      exc,
    )
    if self._deferred is None or not self._deferred.submit(
      strategy=payload.strategy, envelope=envelope, msg_id=msg_id
    ):
      log.error("Enqueue failed and could not be deferred: %s", exc)
      raise SignalError(503, f"Enqueue failed: {exc}")

    return {
      "status": "deferred",
      "timestamp": payload.timestamp.isoformat(),
    }

  # Backwards-compat alias so tests / callers that still say ``.process`` keep
  # working. The webhook route uses ``enqueue`` directly.
  process = enqueue

  # ── Handler path (called from the JetStream consumer) ──────────────

  async def handle_enqueued(self, payload: WebhookPayload) -> Dict[str, Any]:
    """First-time processing of a JetStream envelope.

    Checks the block gate (blocked signals get a notification and are dropped
    without persisting — matches the pre-JetStream 403 behaviour aside from
    the fact TradingView already got its 202), persists the row and delegates
    to the fan-out. Raises on persist failure so JetStream redelivers.
    """
    if await self._is_blocked(payload):
      return {"status": "blocked"}

    signal_id = await self._signals.log_signal(payload)
    if not signal_id:
      raise RuntimeError("Failed to persist signal into database")

    return await self._fanout(
      signal_id=signal_id,
      payload=payload,
      attempts_before=settings.signal.MAX_ATTEMPTS,
    )

  # ── Retry path (called from the retry job) ─────────────────────────

  async def retry_signal(self, signal_id: str) -> Dict[str, Any]:
    """Second / third attempt on an already-persisted QUEUED signal.

    Loads the row, rebuilds the ``WebhookPayload`` from ``row.raw`` and calls
    the same fan-out helper the first attempt uses — the only difference is
    the ``attempts_before`` value, which drives the ``Attempt: N`` line on
    the Telegram notification and the failure decrement.
    """
    row = await self._signals.get_by_id(signal_id)
    if row is None:
      return {"status": "not_found"}
    if row.status != SignalStatusEnum.QUEUED or row.attempts <= 0:
      # Another worker or the retry job itself may have already handled it.
      return {"status": "skipped", "signal_id": signal_id}
    if not isinstance(row.raw, dict):
      log.error("retry_signal id=%s: row has no usable raw payload", signal_id)
      await self._signals.record_attempt_failure(signal_id)
      return {"status": "invalid", "signal_id": signal_id}

    try:
      payload = WebhookPayload(**row.raw)
    except Exception as exc:
      log.exception(
        "retry_signal id=%s: cannot rebuild WebhookPayload: %s", signal_id, exc
      )
      await self._signals.record_attempt_failure(signal_id)
      return {"status": "invalid", "signal_id": signal_id}

    return await self._fanout(
      signal_id=signal_id,
      payload=payload,
      attempts_before=row.attempts,
    )

  # ── Shared fan-out ─────────────────────────────────────────────────

  async def _fanout(
    self,
    *,
    signal_id: str,
    payload: WebhookPayload,
    attempts_before: int,
  ) -> Dict[str, Any]:
    """Publish to workers and notify; record success or attempt failure."""
    try:
      if payload.position.action == SignalActionEnum.FLAT:
        await self._publish_flat(payload, signal_id)
      else:
        await self._publish_signal(payload, signal_id)
    except Exception as exc:
      log.exception(
        "Fan-out publish failed signal_id=%s attempts_before=%d: %s",
        signal_id,
        attempts_before,
        exc,
      )
      await self._signals.record_attempt_failure(signal_id)
      return {
        "status": "retry_scheduled",
        "signal_id": signal_id,
        "attempts_before": attempts_before,
      }

    # Broadcast and mark-published are best-effort — they must not roll
    # back a successful worker publish.
    await self._broadcast(payload, attempts_before=attempts_before)
    await self._signals.mark_published(signal_id)

    return {
      "status": "accepted",
      "signal_id": signal_id,
      "timestamp": payload.timestamp.isoformat(),
    }

  # ── Steps ──────────────────────────────────────────────────────────

  def _verify_token(self, payload: WebhookPayload) -> None:
    if not self._webhook_secret:
      raise SignalError(500, "Webhook secret not configured")
    if payload.token != self._webhook_secret:
      log.warning("Invalid token received in webhook payload")
      raise SignalError(401, "Invalid token received in webhook payload")

  async def _is_blocked(self, payload: WebhookPayload) -> bool:
    if await self._settings.get(SIGNAL_BLOCKED) != "1":
      return False
    log.warning("Signal blocked: %s is enabled", SIGNAL_BLOCKED)
    try:
      await self._notifier.send_message(format_blocked_message(payload))
    except Exception as exc:
      log.warning("Blocked-signal notification failed: %s", exc)
    return True

  async def _publish_flat(self, payload: WebhookPayload, signal_id: str) -> None:
    """Publish the FLAT directive with both ids, like a full TradingSignal:
    ``signal_id`` identifies this directive, ``signal_uxid`` the cycle."""
    flat_symbol = payload.symbol.split(":")[-1].upper().strip()
    await self._publisher.publish_flat(
      signal_id=signal_id,
      signal_uxid=payload.signal_uxid,
      symbol=flat_symbol,
      timestamp=payload.timestamp,
      strategy=payload.strategy,
    )

  async def _publish_signal(self, payload: WebhookPayload, signal_id: str) -> None:
    try:
      signal = parse_signal(payload, signal_id)
    except Exception as exc:
      log.exception("Signal parse error for %s: %s", payload.symbol, exc)
      # A parse failure is not transient — bubble a sentinel exception so the
      # caller records the attempt and stops trying.
      raise

    await self._publisher.publish(signal=signal)

  async def _broadcast(self, payload: WebhookPayload, *, attempts_before: int) -> None:
    """Fold the signal into its cycle's Telegram message.

    The service no longer formats anything itself: the broadcaster owns the
    cycle (``strategy`` + ``signal_uxid``), the message ids per chat and the
    rendering, because a signal is a line inside a longer-lived message rather
    than a message of its own.
    """
    if self._broadcaster is None:
      return
    try:
      await self._broadcaster.broadcast(
        payload,
        attempt_number=_attempt_number_for_notification(attempts_before),
      )
    except Exception as exc:
      log.warning("Signal broadcast failed: %s", exc)


class SignalWorker:
  """Pulls JetStream envelopes and hands them to ``SignalProcessingService``."""

  def __init__(
    self,
    service: SignalProcessingService,
    connection: NatsClient | None = None,
  ) -> None:
    self._service = service
    self._conn = connection or nats_client
    self._task: Optional[asyncio.Task] = None
    self._stop = asyncio.Event()
    self._sub = None

  async def start(self) -> None:
    """Create the durable pull consumer and launch the background fetch loop."""
    try:
      self._sub = await self._conn.js.pull_subscribe(
        subject=JETSTREAM_SIGNAL_SUBJECT_FILTER,
        durable=settings.jetstream.SIGNAL_CONSUMER,
        stream=JETSTREAM_SIGNAL_STREAM,
      )
    except BadRequestError as exc:
      log.error(
        "Failed to create JetStream pull consumer '%s': %s",
        settings.jetstream.SIGNAL_CONSUMER,
        exc,
      )
      raise

    self._stop.clear()
    self._task = asyncio.create_task(self._run(), name="signal-worker")
    log.info(
      "JetStream signal worker started stream=%s consumer=%s subject=%s",
      JETSTREAM_SIGNAL_STREAM,
      settings.jetstream.SIGNAL_CONSUMER,
      JETSTREAM_SIGNAL_SUBJECT_FILTER,
    )

  async def stop(self) -> None:
    """Stop the fetch loop and wait for the current message to finish."""
    self._stop.set()
    if self._task is not None:
      try:
        await self._task
      except asyncio.CancelledError:
        pass
      self._task = None
    if self._sub is not None:
      try:
        await self._sub.unsubscribe()
      except Exception as exc:
        log.warning("Failed to unsubscribe JetStream signal worker: %s", exc)
    log.info("JetStream signal worker stopped.")

  async def _run(self) -> None:
    while not self._stop.is_set():
      try:
        msgs = await self._sub.fetch(
          settings.jetstream.FETCH_BATCH,
          timeout=settings.jetstream.FETCH_TIMEOUT_SECONDS,
        )
      except NatsTimeoutError:
        continue
      except asyncio.CancelledError:
        raise
      except Exception as exc:
        log.exception("JetStream fetch failed: %s", exc)
        # Back off briefly on unexpected errors so we don't spin at 100% CPU
        # if the connection is in a broken state; ``stop`` breaks out early.
        await asyncio.sleep(1.0)
        continue

      for msg in msgs:
        await self._handle_one(msg)

  async def _handle_one(self, msg) -> None:
    """Deserialize one envelope, run the pipeline, ack or NAK accordingly."""
    try:
      envelope = json.loads(msg.data.decode())
    except json.JSONDecodeError as exc:
      log.error("SIGNAL envelope: malformed JSON: %s | raw=%s", exc, msg.data)
      # A malformed envelope will never become valid — term instead of NAK so
      # JetStream stops redelivering it.
      await self._term(msg)
      return

    raw_payload = envelope.get("payload") if isinstance(envelope, dict) else None
    if not isinstance(raw_payload, dict):
      log.error("SIGNAL envelope: missing payload: %s", envelope)
      await self._term(msg)
      return

    try:
      payload = WebhookPayload(**raw_payload)
    except ValidationError as exc:
      log.error("SIGNAL envelope: invalid WebhookPayload: %s", exc)
      await self._term(msg)
      return

    try:
      # ``handle_enqueued`` records per-attempt failures onto the persisted
      # row itself (attempts / last_attempt / FAILED). The JetStream message
      # is still ack-ed on the happy path AND on those recorded failures — a
      # NAK here would only cause JetStream to redeliver, which would race
      # the DB-tracked retry job. We only NAK for exceptions that fell out
      # of the service entirely (e.g. persist failed before an attempt was
      # even recorded), so JetStream can retry the persist itself.
      await self._service.handle_enqueued(payload=payload)
    except Exception as exc:
      log.exception("SIGNAL handler failed: %s — leaving for JetStream redelivery", exc)
      await self._nak(msg)
      return

    await self._ack(msg)

  async def _ack(self, msg) -> None:
    try:
      await msg.ack()
    except Exception as exc:
      log.warning("Failed to ack JetStream message: %s", exc)

  async def _nak(self, msg) -> None:
    try:
      await msg.nak()
    except Exception as exc:
      log.warning("Failed to nak JetStream message: %s", exc)

  async def _term(self, msg) -> None:
    try:
      await msg.term()
    except Exception as exc:
      log.warning("Failed to term JetStream message: %s", exc)
