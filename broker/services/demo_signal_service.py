"""
broker/services/demo_signal_service.py — the drill subject: Telegram, nothing else.

``DemoSignalConsumer`` subscribes to core NATS ``SIGNALS_DEMO.>`` and treats
each envelope as a full ``WebhookPayload``, exactly as the JetStream handler
does. What it does with it is deliberately a strict subset of
``SignalProcessingService``:

* it records the signal onto its broadcast cycle, so the Telegram message goes
  out through the real renderer and reads like a real signal — which is the
  whole point, since what the drill proves is that the notification path works
  for that strategy;
* it posts one line to the operator channel saying a drill arrived, so a
  broker-log reader can tell this card apart from a trade;
* and it stops there.

**Three things it does not do, and why each is a safety property rather than a
simplification:**

* **No publish.** This service is not given a ``SignalPublisher`` at all. It
  cannot command a worker because it holds nothing that could — the guarantee
  does not depend on a branch being taken correctly.
* **No ``signals`` row.** A drill is not persisted. That is not about saving a
  write: ``SystemEventConsumer._build_retry_signals`` replays every signal
  persisted in the last ``max_retry_timeout`` seconds into the next worker
  handshake, so a demo row would be handed to a worker minutes later, by the
  one path that bypasses this service entirely.
* **No block gate, no retry, no attempt counter.** There is nothing to retry: a
  drill that did not arrive is a drill to run again, and the engine says so on
  its own console.

Who may publish here is the cluster's question, as it is for
``SIGNALS.<strategy>``: the demo subject carries the payload's ``token`` and
does not check it, because the HTTP endpoint is the path that verifies tokens
and reaching this subject already means holding NATS credentials. Keep the
broker's NATS private, exactly as the signal subject requires.
"""

from __future__ import annotations

import json
from typing import Optional

from nats.aio.subscription import Subscription
from pydantic import ValidationError

from broker.helpers.message_formatter import format_demo_signal_message
from broker.interfaces import Notifier, SignalBroadcaster
from broker.logger import get_logger
from broker.nats import DEMO_SIGNAL_SUBJECT_FILTER, NatsClient, nats_client
from broker.schemas.webhook_schema import WebhookPayload

log = get_logger(__name__)


class DemoSignalConsumer:
  """Renders demo signals to Telegram. Holds no publisher, by design."""

  def __init__(
    self,
    *,
    broadcaster: SignalBroadcaster | None = None,
    notifier: Notifier | None = None,
    connection: NatsClient | None = None,
  ) -> None:
    # Subscriber-facing channel: the same cycle writer a real signal goes
    # through, so the card is rendered by the same code and a drill that looks
    # right really does mean the renderer works.
    self._broadcaster = broadcaster
    # Operational channel (broker-log chats): one line per drill, so the
    # operator reading the log can see the card was a drill.
    self._notifier = notifier
    self._conn = connection or nats_client
    self._sub: Optional[Subscription] = None

  async def start(self) -> None:
    """Subscribe to the demo subject on the shared core-NATS connection."""
    self._sub = await self._conn.nc.subscribe(
      DEMO_SIGNAL_SUBJECT_FILTER, cb=self.handle_demo_signal
    )
    log.info(
      "NATS demo signal listener subscribed to '%s' (Telegram only, never fanned out)",
      DEMO_SIGNAL_SUBJECT_FILTER,
    )

  async def stop(self) -> None:
    """Unsubscribe from the demo subject."""
    if self._sub is not None:
      try:
        await self._sub.unsubscribe()
      except Exception as exc:
        log.warning("Failed to unsubscribe demo signal listener: %s", exc)
      self._sub = None
    log.info("NATS demo signal listener stopped.")

  async def handle_demo_signal(self, msg) -> None:
    """Validate one drill envelope and notify. Never raises into nats-py.

    A malformed drill is a finding worth a log line and nothing more: core
    NATS has no redelivery to protect, and the operator is watching the engine
    console that published it.
    """
    payload = self._parse(msg)
    if payload is None:
      return

    log.info(
      "Demo signal received subject=%s strategy=%s symbol=%s action=%s signal_uxid=%s "
      "— notifying Telegram, forwarding to no worker",
      msg.subject,
      payload.strategy,
      payload.symbol,
      payload.position.action.value,
      payload.signal_uxid,
    )

    await self._broadcast(payload)
    await self._notify(payload)

  # ── Steps ──────────────────────────────────────────────────────────

  def _parse(self, msg) -> WebhookPayload | None:
    """The envelope's ``payload`` as a ``WebhookPayload``, or None with a reason."""
    try:
      envelope = json.loads(msg.data.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
      log.error("DEMO envelope on %s: malformed JSON: %s", msg.subject, exc)
      return None

    raw_payload = envelope.get("payload") if isinstance(envelope, dict) else None
    if not isinstance(raw_payload, dict):
      log.error("DEMO envelope on %s: missing payload: %s", msg.subject, envelope)
      return None

    try:
      return WebhookPayload(**raw_payload)
    except ValidationError as exc:
      # Worth an error rather than a warning: the drill exists to catch exactly
      # this — an engine building a payload the broker would have rejected.
      log.error("DEMO envelope on %s: invalid WebhookPayload: %s", msg.subject, exc)
      return None

  async def _broadcast(self, payload: WebhookPayload) -> None:
    """Record the drill on its cycle so the dispatcher renders and delivers it."""
    if self._broadcaster is None:
      return
    try:
      await self._broadcaster.broadcast(payload)
    except Exception as exc:
      log.warning("Demo signal broadcast failed: %s", exc)

  async def _notify(self, payload: WebhookPayload) -> None:
    """Tell the operator channel a drill came through, not a trade."""
    if self._notifier is None:
      return
    try:
      await self._notifier.send_message(format_demo_signal_message(payload))
    except Exception as exc:
      log.warning("Demo signal operator notification failed: %s", exc)
