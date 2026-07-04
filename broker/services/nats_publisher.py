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
from broker.nats import NatsClient, nats_client
from broker.schemas.core import SignalActionEnum
from broker.schemas.publisher_schema import (
  AdminSignal,
  PublishTopicEnum,
  SystemCryptoLeverageInitSignal,
  SystemWorkerConnectedAck,
  SystemWorkerConnectedError,
  TradingSignal,
)

log = get_logger(__name__)


class NatsPublisher:
  """Publishes trading signals and FLAT directives to subscribers."""

  def __init__(self, connection: NatsClient | None = None) -> None:
    self._conn = connection or nats_client

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
