"""
broker/services/signal_processing_service.py — Orchestrates the inbound webhook
flow: authenticate, check the block switch, persist, publish, notify.

The service depends only on abstractions (``SignalRepository``,
``SettingRepository``, ``SignalPublisher``, ``Notifier``) so it can be unit
tested with in-memory fakes. It raises the framework-agnostic ``SignalError``;
the HTTP layer translates that into an ``HTTPException``.
"""

from __future__ import annotations

from typing import Any, Dict

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
from broker.schemas.core import SignalActionEnum
from broker.schemas.webhook_schema import WebhookPayload

log = get_logger(__name__)


class SignalError(Exception):
  """Framework-agnostic error carrying an HTTP-ish status code and detail."""

  def __init__(self, status_code: int, detail: str) -> None:
    super().__init__(detail)
    self.status_code = status_code
    self.detail = detail


class SignalProcessingService:
  """Coordinates persistence, publishing, and notification for a webhook."""

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

  async def process(self, payload: WebhookPayload) -> Dict[str, Any]:
    """Run the full webhook pipeline and return the response body."""
    self._verify_token(payload)
    await self._ensure_not_blocked(payload)

    db_signal_id = await self._signals.log_signal(payload)
    if not db_signal_id:
      raise SignalError(500, "Failed to persist signal into database")

    if payload.position.action == SignalActionEnum.FLAT:
      return await self._handle_flat(payload, db_signal_id)

    return await self._handle_signal(payload, db_signal_id)

  # ── Steps ──────────────────────────────────────────────────────────

  def _verify_token(self, payload: WebhookPayload) -> None:
    if not self._webhook_secret:
      raise SignalError(500, "Webhook secret not configured")
    if payload.token != self._webhook_secret:
      log.warning("Invalid token received in webhook payload")
      raise SignalError(401, "Invalid token received in webhook payload")

  async def _ensure_not_blocked(self, payload: WebhookPayload) -> None:
    if await self._settings.get(SIGNAL_BLOCKED) != "1":
      return
    log.warning("Signal blocked: %s is enabled", SIGNAL_BLOCKED)
    await self._notifier.send_message(format_blocked_message(payload))
    raise SignalError(403, "Signal processing is currently disabled")

  async def _handle_flat(
    self, payload: WebhookPayload, db_signal_id: str
  ) -> Dict[str, Any]:
    flat_symbol = payload.symbol.split(":")[-1].upper().strip()
    try:
      await self._publisher.publish_flat(
        symbol=flat_symbol,
        timestamp=payload.timestamp,
        strategy=payload.strategy,
      )
    except Exception as exc:
      log.exception("NATS publish_flat error: %s", exc)
      raise SignalError(500, f"Signal logged but publish failed: {exc}")

    timezone_offset = await self._settings.get(NOTIFICATION_TIMEZONE_KEY)
    await self._notifier.send_message(
      format_flat_message(payload, timezone_offset=timezone_offset)
    )
    return {
      "status": "accepted",
      "signal_id": db_signal_id,
      "timestamp": payload.timestamp.isoformat(),
    }

  async def _handle_signal(
    self, payload: WebhookPayload, db_signal_id: str
  ) -> Dict[str, Any]:
    try:
      signal = parse_signal(payload, db_signal_id)
    except Exception as exc:
      log.exception("Signal parse error for %s: %s", payload.symbol, exc)
      raise SignalError(422, str(exc) or "Signal could not be parsed.")

    try:
      await self._publisher.publish(signal=signal)
    except Exception as exc:
      log.exception("NATS publish error: %s", exc)
      raise SignalError(500, f"Signal logged but publish failed: {exc}")

    include_raw = await self._settings.get(NOTIFICATION_INCLUDE_SIGNAL_RAW) == "1"
    timezone_offset = await self._settings.get(NOTIFICATION_TIMEZONE_KEY)
    await self._notifier.send_message(
      format_signal_message(
        payload, include_raw=include_raw, timezone_offset=timezone_offset
      )
    )
    return {
      "status": "accepted",
      "signal_id": signal.signal_id,
      "timestamp": signal.timestamp.isoformat(),
    }
