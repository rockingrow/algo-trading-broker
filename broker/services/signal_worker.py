"""
broker/services/signal_worker.py — JetStream consumer that runs the webhook
fan-out.

The webhook route only persists the signal and enqueues it on JetStream; this
worker pulls those envelopes back off the stream and does the actual work
(publish to workers, notify, mark PUBLISHED). Splitting the pipeline this way
keeps the HTTP request short enough that TradingView's connection is not held
open across the fan-out — the failure mode we saw as ``server closed the
connection unexpectedly``.

JetStream's own redelivery handles crashes: an envelope is only ``ack``-ed
after ``handle_enqueued`` succeeds, so a broker restart in the middle of
processing replays the message.
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional

from nats.errors import TimeoutError as NatsTimeoutError
from nats.js.errors import BadRequestError
from pydantic import ValidationError

from broker.logger import get_logger
from broker.nats import (
  JETSTREAM_SIGNAL_STREAM,
  JETSTREAM_SIGNAL_SUBJECT_FILTER,
  NatsClient,
  nats_client,
)
from broker.schemas.webhook_schema import WebhookPayload
from broker.services.signal_processing_service import SignalProcessingService

log = get_logger(__name__)

# Consumer name is durable so a broker restart resumes from the same position
# on the stream instead of skipping past unacked messages.
JETSTREAM_SIGNAL_CONSUMER = "broker_signal_handler"

# Batch size and fetch timeout picked for a single-writer webhook: even the
# noisiest TradingView setup rarely bursts more than a handful of alerts a
# second, so 10-per-fetch keeps latency low while amortising the pull round trip.
_FETCH_BATCH = 10
_FETCH_TIMEOUT_SECONDS = 1.0


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
        durable=JETSTREAM_SIGNAL_CONSUMER,
        stream=JETSTREAM_SIGNAL_STREAM,
      )
    except BadRequestError as exc:
      log.error(
        "Failed to create JetStream pull consumer '%s': %s",
        JETSTREAM_SIGNAL_CONSUMER,
        exc,
      )
      raise

    self._stop.clear()
    self._task = asyncio.create_task(self._run(), name="signal-worker")
    log.info(
      "JetStream signal worker started stream=%s consumer=%s subject=%s",
      JETSTREAM_SIGNAL_STREAM,
      JETSTREAM_SIGNAL_CONSUMER,
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
        msgs = await self._sub.fetch(_FETCH_BATCH, timeout=_FETCH_TIMEOUT_SECONDS)
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
      log.exception(
        "SIGNAL handler failed: %s — leaving for JetStream redelivery", exc
      )
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
