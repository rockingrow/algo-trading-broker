"""
broker/services/nats_service.py — NATS service layer: outbound publishing and
inbound consumption.

- ``NatsPublisher`` implements the ``SignalPublisher`` Protocol and is the
  only outbound path — clients that only need to publish (e.g. the webhook
  flow) depend on this narrow interface and never see the inbound consumer
  machinery below.

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

  Every valid handshake also records the announced market/gateway on the
  worker's ``accounts`` row (``AccountRepository.upsert_gateway``), so the
  broker can address it as ``<market>-<gateway>-<account_id>`` — notably from
  the admin ``/admin/settings/crypto-*`` push — without waiting for the
  account's first TRADE event.

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
  are combined into a single ``get_many`` query and cached briefly
  (``CRYPTO_SETTINGS_CACHE_TTL_SECONDS``) so that burst doesn't turn into one
  DB round trip per worker.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
  from broker.services.trade_broadcast_service import TradeBroadcastService

from nats.aio.subscription import Subscription
from pydantic import ValidationError

from broker.constants import (
  CRYPTO_ALLOWED_SYMBOL_KEY,
  CRYPTO_MAX_LEVERAGE_KEY,
  DEFAULT_MAX_RETRY_TIMEOUT_SECONDS,
  MAX_RETRY_TIMEOUT_KEY,
)
from broker.helpers.signal_helper import parse_signal
from broker.interfaces import (
  AccountRepository,
  SettingRepository,
  SignalPublisher,
  SignalRepository,
  TradeRepository,
)
from broker.logger import get_logger
from broker.nats import JETSTREAM_SIGNAL_SUBJECT_PREFIX, NatsClient, nats_client
from broker.schemas.account_schema import MarketTypeEnum, decompose_worker_id
from broker.schemas.core import MarketEnum, SignalActionEnum
from broker.schemas.publisher_schema import (
  AdminSignal,
  PublishTopicEnum,
  compose_admin_subject,
  SystemActionEnum,
  SystemCryptoLeverageInitSignal,
  SystemRetrySignal,
  SystemWorkerConnectedAck,
  SystemWorkerConnectedError,
  SystemWorkerConnectedSignal,
  TradingSignal,
)
from broker.schemas.trade_event_schema import PositionEvent
from broker.schemas.webhook_schema import WebhookPayload

log = get_logger(__name__)

def _jetstream_subject(strategy: str) -> str:
  """Return the JetStream subject a webhook envelope should be published on.

  Kept as a helper so producers and consumers agree on the layout without
  hard-coding string concatenation in two places.
  """
  return f"{JETSTREAM_SIGNAL_SUBJECT_PREFIX}.{strategy}"

# nats-py runs one asyncio task per subscription, pulling messages off an
# internal queue and awaiting the callback to completion before pulling the
# next one — WORKER_CONNECTED handshakes on SYSTEM are therefore processed
# one at a time, not concurrently. A reconnect storm (NATS/broker restart)
# can queue up dozens of these back-to-back, so caching the two crypto
# settings briefly keeps that burst from re-reading the DB on every single
# handshake. These settings can be changed via POST /admin/settings/crypto-*,
# so a short TTL is a deliberate trade-off between freshness and load: an
# admin update reaches new handshakes within CRYPTO_SETTINGS_CACHE_TTL_SECONDS.
CRYPTO_SETTINGS_CACHE_TTL_SECONDS = 30.0


class TradeEventConsumer:
  """Consumes TRADE events from NATS and persists them via a TradeRepository.

  When a ``TradeBroadcastService`` is injected, each persisted event is also
  handed to it so a completed (closed) trade is DM-ed to its subscribed
  owners. The broadcast is best-effort and never blocks persistence.
  """

  def __init__(
    self,
    trade_repository: TradeRepository,
    connection: NatsClient | None = None,
    broadcast_service: "TradeBroadcastService | None" = None,
  ) -> None:
    self._repo = trade_repository
    self._conn = connection or nats_client
    self._broadcast = broadcast_service
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
      trade = await self._repo.upsert_by_position_event(event)
    except Exception as exc:
      log.exception("Failed to apply TRADE event: %s", exc)
      return

    if self._broadcast is not None:
      try:
        await self._broadcast.maybe_broadcast(event, trade)
      except Exception as exc:
        # Broadcasting must never break TRADE consumption.
        log.exception("Failed to broadcast completed trade: %s", exc)


class SystemEventConsumer:
  """Consumes SYSTEM events from NATS and responds with CRYPTO_LEVERAGE_INIT."""

  SUBJECT = PublishTopicEnum.SYSTEM

  def __init__(
    self,
    setting_repository: SettingRepository,
    account_repository: AccountRepository,
    publisher: SignalPublisher,
    signal_repository: SignalRepository | None = None,
    connection: NatsClient | None = None,
  ) -> None:
    self._settings = setting_repository
    self._accounts = account_repository
    self._publisher = publisher
    self._signals = signal_repository
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
      "SYSTEM WORKER_CONNECTED account_id=%s market=%s gateway=%s strategies=%s",
      event.account_id,
      event.market,
      event.gateway,
      event.strategies,
    )

    await self._remember_worker(event)

    # Every WORKER_CONNECTED gets a RETRY_SIGNALS replay of the recent signals
    # matching the strategies the worker announced, so a reconnecting worker
    # can catch up on broadcasts it missed while offline. The replay is sent
    # in addition to (never instead of) the ACK / CRYPTO_LEVERAGE_INIT so
    # non-crypto workers get their catch-up too.
    await self._send_retry_signal(event.account_id, event.strategies, reply_to)

    # Only crypto workers need leverage configuration pushed back on connect.
    if event.market != MarketEnum.CRYPTO.value:
      # Acknowledge so a requesting worker's handshake resolves instead of
      # timing out; nothing to configure for non-crypto markets.
      await self._reply_ack(reply_to, event.account_id)
      return

    await self._send_crypto_leverage_init(event.account_id, reply_to)

  async def _remember_worker(self, event: SystemWorkerConnectedSignal) -> None:
    """Store the market/gateway this worker announced on its ``accounts`` row.

    The handshake is the only message that always carries the gateway, and it
    arrives as soon as the worker connects. Recording it here is what lets the
    admin ``/admin/settings/crypto-*`` push address the worker as
    ``<market>-<gateway>-<account_id>``; relying on the TRADE event alone leaves
    a worker that has not traded yet with a NULL gateway and silently skipped.

    ``event.account_id`` is the full worker id, so strip the prefix back to the
    bare account_id the ``accounts`` table is keyed by. Best-effort — a
    bookkeeping failure must not stop the worker's reply.
    """
    account_id = decompose_worker_id(event.account_id, event.market, event.gateway)
    try:
      await self._accounts.upsert_gateway(
        account_id=account_id,
        market=MarketTypeEnum(event.market),
        gateway=event.gateway,
      )
    except Exception as exc:
      log.exception(
        "Failed to record gateway for account_id=%s: %s",
        account_id,
        exc,
      )

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

    if default_leverage <= 0:
      log.error(
        "SYSTEM CRYPTO_LEVERAGE_INIT skipped account_id=%s: %s must be positive, got %r",
        account_id,
        CRYPTO_MAX_LEVERAGE_KEY,
        leverage_raw,
      )
      await self._reply_error(
        reply_to, account_id, f"{CRYPTO_MAX_LEVERAGE_KEY} must be a positive integer"
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

  async def _send_retry_signal(
    self, account_id: str, strategies: list[str], reply_to: str = ""
  ) -> None:
    """Push a RETRY_SIGNALS replay of the last ``max_retry_timeout`` seconds.

    Nothing to do when the worker announced no strategies, or when we have no
    ``SignalRepository`` wired in (the deployment opted out of the replay).
    Query hits are shaped through ``parse_signal`` so the payload matches the
    live SIGNAL messages exactly. Best-effort: a broken lookup, an invalid
    persisted row, or a failed publish is logged and the handshake continues.
    """
    if self._signals is None or not strategies:
      return

    window_seconds = await self._get_max_retry_timeout_seconds()
    try:
      envelopes = await self._signals.list_recent_by_strategies(
        strategies=strategies, since_seconds=window_seconds
      )
    except Exception as exc:
      log.exception(
        "SYSTEM RETRY_SIGNALS skipped account_id=%s: signals lookup failed: %s",
        account_id,
        exc,
      )
      return

    signals: list[TradingSignal] = []
    for envelope in envelopes:
      raw_payload = envelope.get("payload")
      signal_id = envelope.get("signal_id")
      if not isinstance(raw_payload, dict) or not signal_id:
        continue
      try:
        payload = WebhookPayload(**raw_payload)
        signals.append(parse_signal(payload, signal_id))
      except Exception as exc:
        # A single bad row must not derail the replay for the rest.
        log.warning(
          "SYSTEM RETRY_SIGNALS skipping bad row signal_id=%s: %s", signal_id, exc
        )

    try:
      await self._publisher.publish_system_retry_signal(
        account_id=account_id,
        signals=signals,
        subject=reply_to or None,
      )
    except Exception as exc:
      log.exception(
        "Failed to publish RETRY_SIGNALS for account_id=%s: %s", account_id, exc
      )

  async def _get_max_retry_timeout_seconds(self) -> int:
    """Read the ``max_retry_timeout`` broker setting, falling back to the
    default on missing/invalid values."""
    raw = await self._settings.get(MAX_RETRY_TIMEOUT_KEY)
    if raw is None:
      return DEFAULT_MAX_RETRY_TIMEOUT_SECONDS
    try:
      value = int(raw)
    except (TypeError, ValueError):
      log.warning(
        "%s is not an integer: %r — using default %d",
        MAX_RETRY_TIMEOUT_KEY,
        raw,
        DEFAULT_MAX_RETRY_TIMEOUT_SECONDS,
      )
      return DEFAULT_MAX_RETRY_TIMEOUT_SECONDS
    if value <= 0:
      log.warning(
        "%s must be positive, got %r — using default %d",
        MAX_RETRY_TIMEOUT_KEY,
        raw,
        DEFAULT_MAX_RETRY_TIMEOUT_SECONDS,
      )
      return DEFAULT_MAX_RETRY_TIMEOUT_SECONDS
    return value

  async def _get_crypto_settings(self) -> tuple[Optional[str], Optional[str]]:
    """Return (symbols_raw, leverage_raw), reusing a cached read for up to
    ``CRYPTO_SETTINGS_CACHE_TTL_SECONDS``.

    On a cache miss, both settings are fetched with a single ``get_many`` query
    instead of one round trip per key — this is also what makes the read
    atomic: both values reflect the same DB snapshot, so a concurrent
    ``/admin/settings/crypto-*`` write can never land between the two reads.
    Both hits and misses are cached; worst case an operator who just changed a
    setting via the admin API waits up to the TTL for it to reach the next
    handshake.
    """
    now = time.monotonic()
    if (
      self._crypto_settings_cache is not None
      and now - self._crypto_settings_cached_at < CRYPTO_SETTINGS_CACHE_TTL_SECONDS
    ):
      return self._crypto_settings_cache

    values = await self._settings.get_many(
      [CRYPTO_ALLOWED_SYMBOL_KEY, CRYPTO_MAX_LEVERAGE_KEY]
    )
    symbols_raw = values.get(CRYPTO_ALLOWED_SYMBOL_KEY)
    leverage_raw = values.get(CRYPTO_MAX_LEVERAGE_KEY)
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


# ── Outbound side of NATS ────────────────────────────────────────────────


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

  async def publish_flat(
    self,
    *,
    signal_id: str,
    symbol: str,
    timestamp: datetime,
    strategy: str,
  ) -> None:
    """Broadcast a FLAT (close-all) directive on the strategy subject.

    Carries ``signal_id`` — same field the LONG/SHORT/TP payloads (a full
    ``TradingSignal``) already do — so a worker seeing this signal live and
    then again inside a ``SYSTEM.RETRY_SIGNALS`` replay can de-duplicate by
    id instead of by guessing on content.
    """
    payload = json.dumps(
      {
        "signal_id": signal_id,
        "strategy": strategy,
        "timestamp": timestamp.isoformat(),
        "action": SignalActionEnum.FLAT.value,
        "symbol": symbol,
      }
    ).encode()
    await self._conn.nc.publish(strategy, payload)
    log.info(
      "Published [%s] FLAT directive signal_id=%s symbol=%s",
      strategy,
      signal_id,
      symbol,
    )

  async def publish_admin_signal(self, **kwargs) -> None:
    """Publish an admin signal to workers.

    When the signal is account-scoped (``account_id`` set — ``market``/``gateway``
    are required alongside it) it goes to the per-account private subject
    ``ADMIN.<market>.<gateway>.<account_id>``, so only that account's worker
    receives it and no other worker learns the ``account_id``. Otherwise it is
    broadcast on the shared ``ADMIN`` subject for every worker to filter itself.
    """
    signal = AdminSignal(**kwargs)
    if signal.account_id is not None:
      subject = compose_admin_subject(
        signal.market, signal.gateway, signal.account_id
      )
    else:
      subject = PublishTopicEnum.ADMIN.value
    payload = signal.model_dump_json().encode()
    await self._conn.nc.publish(subject, payload)
    log.info(
      "Published [%s] action=%s strategy=%s symbol=%s account_id=%s market=%s gateway=%s",
      subject,
      signal.action,
      signal.strategy,
      signal.symbol,
      signal.account_id,
      signal.market,
      signal.gateway,
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
    """Publish a RETRY_SIGNALS replay of recent signals to a reconnecting worker.

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
