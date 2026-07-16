"""
broker/services/signal_processing_service.py — webhook pipeline, JetStream
handler pipeline, and retry pipeline in one service.

There are three entry points, all built on the same fan-out helper:

* ``enqueue`` — called by the HTTP handler. Verifies the token and pushes the
  raw envelope onto JetStream. Nothing else — no DB write, no block check, no
  publish, no notification. TradingView therefore gets its ``202`` back as
  soon as JetStream ack-s the write, closing the ``server closed the
  connection unexpectedly`` failure mode from holding the request open.

* ``handle_enqueued`` — called by the JetStream consumer. Runs the block
  gate, persists the row (``status=QUEUED``, ``attempts=SIGNAL_MAX_ATTEMPTS``)
  and delegates to ``_fanout``. First attempt on a signal.

* ``retry_signal`` — called by the retry job. Loads the row (already
  persisted), rebuilds the payload from ``row.raw`` and delegates to
  ``_fanout``. Second / third attempt on the same row.

``_fanout`` publishes on the strategy subject and sends the Telegram
notification. On success the row is flipped to ``PUBLISHED``; on failure the
row's ``attempts`` counter is decremented (and turned into ``FAILED`` when it
hits zero) so the retry job knows whether to pick it up again.

The service depends only on abstractions (``SignalRepository``,
``SettingRepository``, ``SignalPublisher``, ``Notifier``) so all three flows
can be exercised with in-memory fakes. It raises ``SignalError`` for HTTP
translation on the enqueue path only.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from broker.constants import (
  NOTIFICATION_INCLUDE_SIGNAL_RAW,
  NOTIFICATION_TIMEZONE_KEY,
  SIGNAL_BLOCKED,
)
from broker.helpers.message_formatter import (
  format_blocked_message,
  format_flat_message,
  format_signal_message,
)
from broker.helpers.signal_helper import parse_signal
from broker.interfaces import (
  Notifier,
  SettingRepository,
  SignalPublisher,
  SignalRepository,
)
from broker.logger import get_logger
from broker.schemas.core import SignalActionEnum, SignalStatusEnum
from broker.schemas.webhook_schema import WebhookPayload
from broker.settings import settings

log = get_logger(__name__)


class SignalError(Exception):
  """Framework-agnostic error carrying an HTTP-ish status code and detail."""

  def __init__(self, status_code: int, detail: str) -> None:
    super().__init__(detail)
    self.status_code = status_code
    self.detail = detail


def _attempt_number_for_notification(attempts_before: int) -> int | None:
  """Sequence number (1-based) of the current attempt for the Telegram line.

  Rendered as ``Attempt: N`` only for the 2nd and 3rd attempt on a signal —
  the first attempt is the default path and doesn't warrant a marker. Returns
  ``None`` when the line should be suppressed.
  """
  if attempts_before > 2:
    return None
  return settings.SIGNAL_MAX_ATTEMPTS - attempts_before + 1


class SignalProcessingService:
  """Coordinates persistence, JetStream enqueue, publishing, and notification."""

  def __init__(
    self,
    *,
    signal_repository: SignalRepository,
    setting_repository: SettingRepository,
    publisher: SignalPublisher,
    notifier: Notifier,
    webhook_secret: str,
  ) -> None:
    self._signals = signal_repository
    self._settings = setting_repository
    self._publisher = publisher
    self._notifier = notifier
    self._webhook_secret = webhook_secret

  # ── Enqueue path (called from the webhook route) ───────────────────

  async def enqueue(self, payload: WebhookPayload) -> Dict[str, Any]:
    """Verify the token and hand the raw envelope to JetStream. Fast path.

    No DB write, no block check, no worker fan-out — everything moved into
    ``handle_enqueued`` so TradingView is not held open across the pipeline.
    """
    self._verify_token(payload)
    envelope = {"payload": payload.model_dump(mode="json")}
    try:
      await self._publisher.publish_webhook_event(
        signal_id="",
        strategy=payload.strategy,
        envelope=envelope,
      )
    except Exception as exc:
      log.exception("JetStream enqueue error: %s", exc)
      raise SignalError(500, f"Enqueue failed: {exc}")

    return {
      "status": "queued",
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
      attempts_before=settings.SIGNAL_MAX_ATTEMPTS,
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

    # Notification and mark-published are best-effort — they must not roll
    # back a successful worker publish.
    await self._send_notification(payload, attempts_before=attempts_before)
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
    flat_symbol = payload.symbol.split(":")[-1].upper().strip()
    await self._publisher.publish_flat(
      signal_id=signal_id,
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

  async def _send_notification(
    self, payload: WebhookPayload, *, attempts_before: int
  ) -> None:
    attempt_number = _attempt_number_for_notification(attempts_before)
    try:
      if payload.position.action == SignalActionEnum.FLAT:
        timezone_offset = await self._settings.get(NOTIFICATION_TIMEZONE_KEY)
        message = format_flat_message(
          payload,
          timezone_offset=timezone_offset,
          attempt_number=attempt_number,
        )
      else:
        include_raw = (
          await self._settings.get(NOTIFICATION_INCLUDE_SIGNAL_RAW) == "1"
        )
        timezone_offset = await self._settings.get(NOTIFICATION_TIMEZONE_KEY)
        message = format_signal_message(
          payload,
          include_raw=include_raw,
          timezone_offset=timezone_offset,
          attempt_number=attempt_number,
        )
      await self._notifier.send_message(message)
    except Exception as exc:
      log.warning("Signal notification failed: %s", exc)


# Convenience getter kept out of the class to make it easier to import from
# tests without instantiating the service.
def attempt_number_for_notification(attempts_before: int) -> Optional[int]:
  return _attempt_number_for_notification(attempts_before)
