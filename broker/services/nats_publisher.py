"""
broker/services/nats_publisher.py — Outbound side of NATS.

Implements the ``SignalPublisher`` Protocol. Clients that only need to publish
(e.g. the webhook flow) depend on this narrow interface and never see the
inbound consumer machinery.
"""

from __future__ import annotations

import json
from datetime import datetime

from broker.logger import get_logger
from broker.nats import JETSTREAM_SIGNAL_SUBJECT_PREFIX, NatsClient, nats_client
from broker.schemas.core import SignalActionEnum
from broker.schemas.publisher_schema import (
  AdminSignal,
  PublishTopicEnum,
  SystemCryptoLeverageInitSignal,
  SystemRetrySignal,
  SystemWorkerConnectedAck,
  SystemWorkerConnectedError,
  TradingSignal,
)

log = get_logger(__name__)


def _jetstream_subject(strategy: str) -> str:
  """Return the JetStream subject a webhook envelope should be published on.

  Kept as a helper so producers and consumers agree on the layout without
  hard-coding string concatenation in two places.
  """
  return f"{JETSTREAM_SIGNAL_SUBJECT_PREFIX}.{strategy}"


class NatsPublisher:
  """Publishes trading signals and FLAT directives to subscribers."""

  def __init__(self, connection: NatsClient | None = None) -> None:
    self._conn = connection or nats_client

  async def publish_webhook_event(
    self, *, signal_id: str, strategy: str, envelope: dict
  ) -> None:
    """Persist a raw webhook envelope to JetStream so it can be handled offline.

    The webhook HTTP path calls this to move the entire signal-handling pipeline
    (parse → publish to workers → notify → mark PUBLISHED) into a background
    consumer. TradingView therefore gets its 202 back as soon as the message is
    durably queued, closing the ``server closed the connection unexpectedly``
    failure mode that came from doing the whole pipeline inline.
    """
    subject = _jetstream_subject(strategy)
    payload = json.dumps(envelope, default=str).encode()
    ack = await self._conn.js.publish(subject, payload)
    log.info(
      "Enqueued [%s] signal_id=%s stream_seq=%s",
      subject,
      signal_id,
      getattr(ack, "seq", None),
    )

  async def publish(self, signal: TradingSignal) -> None:
    """Serialise *signal* and broadcast to subscribers on the strategy subject."""
    if signal is None:
      log.warning("NatsPublisher.publish called with None signal; skipping.")
      return
    subject = signal.strategy
    payload = signal.model_dump_json().encode()
    await self._conn.nc.publish(subject, payload)
    log.info(
      "Published [%s] signal_id=%s action=%s symbol=%s",
      subject,
      signal.signal_id,
      signal.action,
      signal.symbol,
    )

  async def publish_flat(self, symbol: str, timestamp: datetime, strategy: str) -> None:
    """Broadcast a FLAT (close-all) directive on the strategy subject."""
    payload = json.dumps(
      {
        "strategy": strategy,
        "timestamp": timestamp.isoformat(),
        "action": SignalActionEnum.FLAT.value,
        "symbol": symbol,
      }
    ).encode()
    await self._conn.nc.publish(strategy, payload)
    log.info("Published [%s] FLAT directive symbol=%s", strategy, symbol)

  async def publish_admin_signal(self, **kwargs) -> None:
    """Broadcast an admin signal on the ADMIN subject."""
    signal = AdminSignal(**kwargs)
    payload = signal.model_dump_json().encode()
    await self._conn.nc.publish(PublishTopicEnum.ADMIN.value, payload)
    log.info(
      "Published [ADMIN] action=%s strategy=%s symbol=%s account_id=%s",
      signal.action,
      signal.strategy,
      signal.symbol,
      signal.account_id,
    )

  async def publish_system_signal(
    self, *, subject: str | None = None, **kwargs
  ) -> None:
    """Publish a CRYPTO_LEVERAGE_INIT system signal.

    When *subject* is given (a worker's reply inbox from NATS ``request``) the
    signal is delivered directly to that one worker; otherwise it is broadcast
    on the shared SYSTEM subject for backward compatibility with fire-and-forget
    workers.
    """
    signal = SystemCryptoLeverageInitSignal(**kwargs)
    target = subject or PublishTopicEnum.SYSTEM.value
    payload = signal.model_dump_json().encode()
    await self._conn.nc.publish(target, payload)
    log.info(
      "Published [SYSTEM→%s] action=%s account_id=%s symbols=%s default_leverage=%s",
      target,
      signal.action,
      signal.account_id,
      signal.symbols,
      signal.default_leverage,
    )

  async def publish_system_retry_signal(
    self, *, subject: str | None = None, **kwargs
  ) -> None:
    """Publish a RETRY_SIGNAL replay of recent signals to a reconnecting worker.

    Sent as the second half of the WORKER_CONNECTED handshake — after the
    market-specific ACK / CRYPTO_LEVERAGE_INIT — so a worker that missed
    broadcasts while it was offline can catch up. Delivered directly on
    *subject* (the request's reply inbox) when set; otherwise broadcast on the
    shared SYSTEM subject.
    """
    signal = SystemRetrySignal(**kwargs)
    target = subject or PublishTopicEnum.SYSTEM.value
    payload = signal.model_dump_json().encode()
    await self._conn.nc.publish(target, payload)
    log.info(
      "Published [SYSTEM→%s] action=%s account_id=%s count=%d",
      target,
      signal.action,
      signal.account_id,
      len(signal.signals),
    )

  async def publish_system_ack(self, *, subject: str, **kwargs) -> None:
    """Reply on a worker's request inbox acknowledging that no initial
    configuration is required (e.g. non-crypto markets)."""
    signal = SystemWorkerConnectedAck(**kwargs)
    payload = signal.model_dump_json().encode()
    await self._conn.nc.publish(subject, payload)
    log.info(
      "Published [SYSTEM→%s] action=%s account_id=%s",
      subject,
      signal.action,
      signal.account_id,
    )

  async def publish_system_error(self, *, subject: str, **kwargs) -> None:
    """Reply on a worker's request inbox signalling the broker could not build
    the initial configuration; carries a human-readable ``reason``."""
    signal = SystemWorkerConnectedError(**kwargs)
    payload = signal.model_dump_json().encode()
    await self._conn.nc.publish(subject, payload)
    log.warning(
      "Published [SYSTEM→%s] action=%s account_id=%s reason=%s",
      subject,
      signal.action,
      signal.account_id,
      signal.reason,
    )
