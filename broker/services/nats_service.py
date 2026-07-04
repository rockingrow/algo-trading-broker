"""
broker/services/nats_service.py — Inbound side of NATS.

Hosts the consumers that subscribe to NATS subjects and react to incoming
messages:

- ``TradeEventConsumer`` subscribes to the TRADE subject and applies each
  position event to the trades table via an injected ``TradeRepository``. It
  depends on the repository abstraction, not on a concrete persistence
  function.

- ``SystemEventConsumer`` subscribes to the SYSTEM subject and reacts to
  ``WORKER_CONNECTED`` messages published by workers right after they
  successfully connect to NATS. Each event must carry ``account_id`` (the
  worker identifier in ``<market>-<gateway>-<account_id>`` format, e.g.
  ``CRYPTO-BINANCE-7654321``), ``market`` and ``gateway``; messages missing
  any of these are rejected by ``SystemWorkerConnectedSignal`` validation.

  Request/reply vs. fire-and-forget
  ─────────────────────────────────
  Workers should announce themselves with NATS ``request`` and wait for a
  reply. When a message carries a reply inbox (``msg.reply``), the broker
  answers *that one worker* directly with the outcome of the handshake:

  * crypto worker, settings OK   → ``CRYPTO_LEVERAGE_INIT``
  * non-crypto worker            → ``WORKER_CONNECTED_ACK``
  * settings missing/invalid     → ``WORKER_CONNECTED_ERROR`` (with a reason)

  Because every path replies, a worker's ``request`` always resolves instead
  of silently hanging, and the worker can retry on timeout (e.g. if the
  broker was down or restarting when it first announced). The handshake is
  idempotent, so retries are safe.

  For backward compatibility, a plain fire-and-forget ``publish`` (no reply
  inbox) still triggers a ``CRYPTO_LEVERAGE_INIT`` broadcast on the shared
  SYSTEM subject; in that mode failures can only be logged, not signalled
  back to the worker.

  The broker's own outgoing SYSTEM messages are filtered by action so it
  never reacts to them (replies go to private inboxes and are never received
  here).

  nats-py processes one subscription's messages one at a time (a single task
  awaits each callback to completion before pulling the next message off the
  queue), so a reconnect storm serializes its WORKER_CONNECTED handshakes
  rather than running them concurrently. The two crypto BrokerSetting reads
  are cached briefly (``CRYPTO_SETTINGS_CACHE_TTL_SECONDS``) so that burst
  doesn't turn into one DB round trip pair per worker.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

from nats.aio.subscription import Subscription
from pydantic import ValidationError

from broker.interfaces import SettingRepository, SignalPublisher, TradeRepository
from broker.logger import get_logger
from broker.nats import NatsClient, nats_client
from broker.schemas.core import MarketEnum
from broker.schemas.publisher_schema import (
  PublishTopicEnum,
  SystemActionEnum,
  SystemWorkerConnectedSignal,
)
from broker.schemas.trade_event_schema import PositionEvent

log = get_logger(__name__)

CRYPTO_ALLOWED_SYMBOL_KEY = "crypto_allowed_symbol"
CRYPTO_MAX_LEVERAGE_KEY = "crypto_max_leverage"

# nats-py runs one asyncio task per subscription, pulling messages off an
# internal queue and awaiting the callback to completion before pulling the
# next one — WORKER_CONNECTED handshakes on SYSTEM are therefore processed
# one at a time, not concurrently. A reconnect storm (NATS/broker restart)
# can queue up dozens of these back-to-back, so caching the two crypto
# settings briefly keeps that burst from re-reading the DB on every single
# handshake. These settings have no admin-facing update endpoint today, so a
# short TTL is a safe trade-off between freshness and load.
CRYPTO_SETTINGS_CACHE_TTL_SECONDS = 30.0


class TradeEventConsumer:
  """Consumes TRADE events from NATS and persists them via a TradeRepository."""

  def __init__(
    self,
    trade_repository: TradeRepository,
    connection: NatsClient | None = None,
  ) -> None:
    self._repo = trade_repository
    self._conn = connection or nats_client
    self._sub: Optional[Subscription] = None

  async def start(self) -> None:
    """Subscribe to the TRADE subject using the shared NATS connection."""
    self._sub = await self._conn.nc.subscribe(
      self._conn.LISTEN_SUBJECT.value, cb=self.handle_subject_trade
    )
    log.info("NATS trade listener subscribed to '%s'", self._conn.LISTEN_SUBJECT.value)

  async def stop(self) -> None:
    """Unsubscribe from the TRADE subject."""
    if self._sub is not None:
      try:
        await self._sub.unsubscribe()
      except Exception as exc:
        log.warning("Failed to unsubscribe TRADE listener: %s", exc)
    log.info("NATS trade listener stopped.")

  async def handle_subject_trade(self, msg) -> None:
    """Handle incoming TRADE events from the NATS subject."""
    raw = msg.data.decode()
    try:
      data = json.loads(raw)
      event = PositionEvent(**data)
    except json.JSONDecodeError as exc:
      log.error("TRADE listener: malformed JSON: %s | raw=%s", exc, raw)
      return
    except ValidationError as exc:
      log.error("TRADE listener: invalid PositionEvent: %s | raw=%s", exc, raw)
      return

    log.info(
      "TRADE event=%s account_id=%s ref_id=%s status=%s",
      event.event,
      event.account_id,
      event.ref_source_id,
      event.status,
    )
    try:
      await self._repo.upsert_by_position_event(event)
    except Exception as exc:
      log.exception("Failed to apply TRADE event: %s", exc)


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
    self._crypto_settings_cache: tuple[Optional[str], Optional[str]] | None = None
    self._crypto_settings_cached_at: float = 0.0

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
    """Handle an inbound SYSTEM event.

    When the message carries a reply inbox (``msg.reply`` — the worker used NATS
    ``request``) every outcome is answered on that inbox so the worker's request
    resolves and it can retry on timeout. Without a reply inbox the broker falls
    back to broadcasting ``CRYPTO_LEVERAGE_INIT`` on the SYSTEM subject.
    """
    raw = msg.data.decode()
    reply_to = getattr(msg, "reply", "") or ""

    try:
      data = json.loads(raw)
    except json.JSONDecodeError as exc:
      log.error("SYSTEM listener: malformed JSON: %s | raw=%s", exc, raw)
      await self._reply_error(reply_to, None, "malformed JSON")
      return

    if not isinstance(data, dict):
      # Valid JSON but not an object (e.g. a bare array or scalar); guard the
      # .get() below so a stray payload can't crash the subscription callback.
      log.error(
        "SYSTEM listener: expected a JSON object, got %s | raw=%s",
        type(data).__name__,
        raw,
      )
      await self._reply_error(reply_to, None, "malformed JSON")
      return

    if data.get("action") != SystemActionEnum.WORKER_CONNECTED.value:
      # Ignore our own outgoing messages (CRYPTO_LEVERAGE_INIT and the ACK/ERROR
      # replies) and unknown actions; the broker only reacts to worker connect
      # announcements. Peeking at the action first avoids logging validation
      # errors for those.
      return

    try:
      event = SystemWorkerConnectedSignal(**data)
    except ValidationError as exc:
      log.error("SYSTEM listener: invalid WORKER_CONNECTED: %s | raw=%s", exc, raw)
      await self._reply_error(
        reply_to, data.get("account_id"), "invalid WORKER_CONNECTED payload"
      )
      return

    log.info(
      "SYSTEM WORKER_CONNECTED account_id=%s market=%s gateway=%s",
      event.account_id,
      event.market,
      event.gateway,
    )

    # Only crypto workers need leverage configuration pushed back on connect.
    if event.market != MarketEnum.CRYPTO.value:
      # Acknowledge so a requesting worker's handshake resolves instead of
      # timing out; nothing to configure for non-crypto markets.
      await self._reply_ack(reply_to, event.account_id)
      return

    await self._send_crypto_leverage_init(event.account_id, reply_to)

  async def _send_crypto_leverage_init(
    self, account_id: str, reply_to: str = ""
  ) -> None:
    """Load crypto settings and deliver CRYPTO_LEVERAGE_INIT for *account_id*.

    Replies on *reply_to* when set (request/reply), otherwise broadcasts on the
    SYSTEM subject. Missing or invalid settings produce an error reply so a
    requesting worker is not left waiting.
    """
    symbols_raw, leverage_raw = await self._get_crypto_settings()

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
      await self._reply_error(reply_to, account_id, "crypto settings not configured")
      return

    symbols = [s.strip() for s in symbols_raw.split(",") if s.strip()]
    try:
      default_leverage = int(leverage_raw)
    except ValueError:
      log.error(
        "SYSTEM CRYPTO_LEVERAGE_INIT skipped account_id=%s: %s is not an int: %r",
        account_id,
        CRYPTO_MAX_LEVERAGE_KEY,
        leverage_raw,
      )
      await self._reply_error(
        reply_to, account_id, f"{CRYPTO_MAX_LEVERAGE_KEY} is not an integer"
      )
      return

    try:
      await self._publisher.publish_system_signal(
        action=SystemActionEnum.CRYPTO_LEVERAGE_INIT,
        account_id=account_id,
        symbols=symbols,
        default_leverage=default_leverage,
        subject=reply_to or None,
      )
    except Exception as exc:
      # NATS itself is unhappy, so we cannot reach the worker on the reply inbox
      # either. Let the worker's request time out and retry rather than masking
      # the failure behind a best-effort error reply that would also fail.
      log.exception(
        "Failed to publish CRYPTO_LEVERAGE_INIT for account_id=%s: %s",
        account_id,
        exc,
      )

  async def _get_crypto_settings(self) -> tuple[Optional[str], Optional[str]]:
    """Return (symbols_raw, leverage_raw), reusing a cached read for up to
    ``CRYPTO_SETTINGS_CACHE_TTL_SECONDS``.

    On a cache miss, both settings are fetched concurrently via
    ``asyncio.gather`` instead of two sequential round trips. Both hits and
    misses are cached (there is no admin endpoint that updates these settings
    today, so treating "not configured" as cacheable too keeps the fix
    simple); worst case an operator who just fixed the DB row waits up to the
    TTL for it to take effect on the next handshake.
    """
    now = time.monotonic()
    if (
      self._crypto_settings_cache is not None
      and now - self._crypto_settings_cached_at < CRYPTO_SETTINGS_CACHE_TTL_SECONDS
    ):
      return self._crypto_settings_cache

    symbols_raw, leverage_raw = await asyncio.gather(
      self._settings.get(CRYPTO_ALLOWED_SYMBOL_KEY),
      self._settings.get(CRYPTO_MAX_LEVERAGE_KEY),
    )
    self._crypto_settings_cache = (symbols_raw, leverage_raw)
    self._crypto_settings_cached_at = now
    return self._crypto_settings_cache

  async def _reply_ack(self, reply_to: str, account_id: str) -> None:
    """Acknowledge a handshake that needs no configuration. No-op when there is
    no reply inbox (fire-and-forget publish)."""
    if not reply_to:
      return
    try:
      await self._publisher.publish_system_ack(subject=reply_to, account_id=account_id)
    except Exception as exc:
      log.warning(
        "Failed to reply WORKER_CONNECTED_ACK account_id=%s: %s", account_id, exc
      )

  async def _reply_error(
    self, reply_to: str, account_id: Optional[str], reason: str
  ) -> None:
    """Tell the worker its handshake could not be fulfilled. No-op when there is
    no reply inbox (fire-and-forget publish)."""
    if not reply_to:
      return
    try:
      await self._publisher.publish_system_error(
        subject=reply_to, account_id=account_id, reason=reason
      )
    except Exception as exc:
      log.warning(
        "Failed to reply WORKER_CONNECTED_ERROR account_id=%s: %s", account_id, exc
      )
