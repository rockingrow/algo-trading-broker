"""
broker/services/nats_system_consumer.py — Inbound side of the SYSTEM subject.

Subscribes to the SYSTEM subject and reacts to ``WORKER_CONNECTED`` messages
published by workers right after they successfully connect to NATS. The
worker's identifier is carried in the ``account_id`` field in the
``<market>-<account_id>`` format (e.g. ``BINANCE-7654321``).

For each ``WORKER_CONNECTED`` event the broker loads the
``crypto_allowed_symbol`` and ``crypto_max_leverage`` BrokerSetting rows and
publishes back a ``CRYPTO_LEVERAGE_INIT`` SystemSignal on the SYSTEM subject
so the worker can apply that configuration.

The broker also publishes ``CRYPTO_LEVERAGE_INIT`` on the SYSTEM subject, so
the consumer filters by action to ignore its own outgoing messages.
"""

from __future__ import annotations

import json
from typing import Optional

from nats.aio.subscription import Subscription
from pydantic import ValidationError

from broker.interfaces import SettingRepository, SignalPublisher
from broker.logger import get_logger
from broker.nats import NatsClient, nats_client
from broker.schemas.publisher_schema import (
  PublishTopicEnum,
  SystemActionEnum,
  SystemSignal,
)

log = get_logger(__name__)

CRYPTO_ALLOWED_SYMBOL_KEY = "crypto_allowed_symbol"
CRYPTO_MAX_LEVERAGE_KEY = "crypto_max_leverage"


class SystemEventConsumer:
  """Consumes SYSTEM events from NATS and responds with CRYPTO_LEVERAGE_INIT."""

  SUBJECT = PublishTopicEnum.SYSTEM

  def __init__(
    self,
    setting_repository: SettingRepository,
    publisher: SignalPublisher,
    connection: NatsClient | None = None,
  ) -> None:
    self._settings = setting_repository
    self._publisher = publisher
    self._conn = connection or nats_client
    self._sub: Optional[Subscription] = None

  async def start(self) -> None:
    """Subscribe to the SYSTEM subject using the shared NATS connection."""
    self._sub = await self._conn.nc.subscribe(
      self.SUBJECT.value, cb=self.handle_subject_system
    )
    log.info("NATS system listener subscribed to '%s'", self.SUBJECT.value)

  async def stop(self) -> None:
    """Unsubscribe from the SYSTEM subject."""
    if self._sub is not None:
      try:
        await self._sub.unsubscribe()
      except Exception as exc:
        log.warning("Failed to unsubscribe SYSTEM listener: %s", exc)
    log.info("NATS system listener stopped.")

  async def handle_subject_system(self, msg) -> None:
    """Handle incoming SYSTEM events from the NATS subject."""
    raw = msg.data.decode()
    try:
      data = json.loads(raw)
      event = SystemSignal(**data)
    except json.JSONDecodeError as exc:
      log.error("SYSTEM listener: malformed JSON: %s | raw=%s", exc, raw)
      return
    except ValidationError as exc:
      log.error("SYSTEM listener: invalid SystemSignal: %s | raw=%s", exc, raw)
      return

    if event.action != SystemActionEnum.WORKER_CONNECTED.value:
      # Ignore our own outgoing messages (CRYPTO_LEVERAGE_INIT) and unknown
      # actions; the broker only reacts to worker connect announcements.
      return

    log.info("SYSTEM WORKER_CONNECTED account_id=%s", event.account_id)
    await self._send_crypto_leverage_init(event.account_id)

  async def _send_crypto_leverage_init(self, account_id: str) -> None:
    """Load crypto settings and publish CRYPTO_LEVERAGE_INIT for *account_id*."""
    symbols_raw = await self._settings.get(CRYPTO_ALLOWED_SYMBOL_KEY)
    leverage_raw = await self._settings.get(CRYPTO_MAX_LEVERAGE_KEY)

    if symbols_raw is None or leverage_raw is None:
      log.warning(
        "SYSTEM CRYPTO_LEVERAGE_INIT skipped account_id=%s: "
        "missing settings (%s=%r, %s=%r)",
        account_id,
        CRYPTO_ALLOWED_SYMBOL_KEY,
        symbols_raw,
        CRYPTO_MAX_LEVERAGE_KEY,
        leverage_raw,
      )
      return

    symbols = [s.strip() for s in symbols_raw.split(",") if s.strip()]
    try:
      default_leverage = int(leverage_raw)
    except ValueError:
      log.error(
        "SYSTEM CRYPTO_LEVERAGE_INIT skipped account_id=%s: "
        "%s is not an int: %r",
        account_id,
        CRYPTO_MAX_LEVERAGE_KEY,
        leverage_raw,
      )
      return

    try:
      await self._publisher.publish_system_signal(
        action=SystemActionEnum.CRYPTO_LEVERAGE_INIT,
        account_id=account_id,
        symbols=symbols,
        default_leverage=default_leverage,
      )
    except Exception as exc:
      log.exception(
        "Failed to publish CRYPTO_LEVERAGE_INIT for account_id=%s: %s",
        account_id,
        exc,
      )
