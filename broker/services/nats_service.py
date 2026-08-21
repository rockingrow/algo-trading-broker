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
  answers *that one worker* directly with **exactly one** message:

  * config OK                     → ``WORKER_CONNECTED_ACK``
  * crypto config missing/invalid → ``WORKER_CONNECTED_ERROR`` (with a reason)

  One and only one, because that is all a reply inbox accepts: ``request()``
  resolves its future (or, ``old_style``, auto-unsubscribes at ``max_msgs=1``)
  on the first reply and silently drops anything after it. So the ACK carries
  the worker's entire initial configuration in a single payload — the
  ``strategy_magic_map`` filtered to the strategies it announced, the
  ``retry_signals`` replay, the ``settings`` its owner set from the bot (the
  ``accounts.settings`` blob, e.g. ``signal_blocked`` from /prevent), and
  (crypto only) the ``crypto_leverage_init`` block. A crypto worker whose
  broker-wide crypto settings are missing or invalid gets the ERROR instead:
  it is not told the handshake succeeded when the config it needs could not be
  built.

  Because every path replies, a worker's ``request`` always resolves instead
  of silently hanging, and the worker can retry on timeout (e.g. if the
  broker was down or restarting when it first announced). The handshake is
  idempotent, so retries are safe.

  For backward compatibility, a plain fire-and-forget ``publish`` (no reply
  inbox) gets the same ACK broadcast on the shared SYSTEM subject, which the
  worker filters by ``account_id``; in that mode failures can only be logged,
  not signalled back to the worker.

  The broker's own outgoing SYSTEM messages are filtered by action so it
  never reacts to them (replies go to private inboxes and are never received
  here).

  nats-py processes one subscription's messages one at a time (a single task
  awaits each callback to completion before pulling the next message off the
  queue), so a reconnect storm serializes its WORKER_CONNECTED handshakes
  rather than running them concurrently. The two crypto BrokerSetting reads
  are combined into a single ``get_many`` query and cached briefly
  (``CRYPTO_SETTINGS_CACHE_TTL_SECONDS``) so that burst doesn't turn into one
  DB round trip per worker. The per-account ``settings`` read is deliberately
  left uncached — it is scoped to one account, so caching it would only ever
  serve the same worker reconnecting twice, at the cost of replying with a
  block that a command run in the meantime has already invalidated.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
  from broker.services.broadcast_service import SignalBroadcastService
  from broker.services.trade_card_service import TradeCardService

from nats.aio.subscription import Subscription
from nats.js import api
from pydantic import ValidationError

from broker.constants import (
  CRYPTO_ALLOWED_SYMBOL_KEY,
  CRYPTO_MAX_LEVERAGE_KEY,
  DEFAULT_MAX_RETRY_TIMEOUT_SECONDS,
  MAX_RETRY_TIMEOUT_KEY,
  STRATEGY_MAGIC_MAP_KEY,
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
from broker.schemas.account_schema import (
  AccountSettings,
  MarketTypeEnum,
  decompose_worker_id,
)
from broker.schemas.core import MarketEnum, SignalActionEnum
from broker.schemas.publisher_schema import (
  AdminSignal,
  CryptoLeverageConfig,
  PublishTopicEnum,
  compose_admin_subject,
  SystemActionEnum,
  SystemCryptoLeverageInitSignal,
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


def _parse_account_settings(raw: object, account_id: str) -> AccountSettings:
  """Shape an ``accounts.settings`` blob into the ACK's ``settings`` block.

  Anything unusable — a row that predates the column, a hand-edited value of
  the wrong type, a key whose value doesn't fit its field — falls back to the
  schema defaults rather than raising: a bad blob must not cost the worker its
  whole configuration, and "no settings" is exactly what the defaults mean.
  Unknown keys are dropped by the model itself (``extra="ignore"``); they stay
  in the row, since writes merge rather than replace.
  """
  if not isinstance(raw, dict):
    if raw is not None:
      log.warning(
        "accounts.settings for account_id=%s is not an object, got %s — using defaults",
        account_id,
        type(raw).__name__,
      )
    return AccountSettings()
  try:
    return AccountSettings(**raw)
  except ValidationError as exc:
    log.error(
      "accounts.settings for account_id=%s is invalid: %s | raw=%r — using defaults",
      account_id,
      exc,
      raw,
    )
    return AccountSettings()


def _parse_strategy_magic_map(raw: Optional[str]) -> dict[str, int]:
  """Parse the ``strategy_magic_map`` JSON-text setting into {strategy: magic}.

  Returns an empty map for a missing/blank value, invalid JSON, or a non-object
  — anything unparseable is logged rather than raised, because the handshake
  must still send the mandatory ``strategy_magic_map`` (an empty map is a valid
  answer). Entries whose value isn't a plain integer are dropped individually
  (booleans are rejected even though ``bool`` is an ``int`` subclass — a magic
  number is never True/False).
  """
  if not raw:
    return {}
  try:
    data = json.loads(raw)
  except (json.JSONDecodeError, TypeError) as exc:
    log.error("%s is not valid JSON: %s | raw=%r", STRATEGY_MAGIC_MAP_KEY, exc, raw)
    return {}
  if not isinstance(data, dict):
    log.error(
      "%s must be a JSON object, got %s | raw=%r",
      STRATEGY_MAGIC_MAP_KEY,
      type(data).__name__,
      raw,
    )
    return {}
  result: dict[str, int] = {}
  for key, value in data.items():
    if isinstance(value, bool) or not isinstance(value, int):
      log.warning(
        "%s: skipping non-integer magic for strategy=%r value=%r",
        STRATEGY_MAGIC_MAP_KEY,
        key,
        value,
      )
      continue
    result[str(key)] = value
  return result


class TradeEventConsumer:
  """Consumes TRADE events from NATS and persists them via a TradeRepository.

  When a ``TradeCardService`` is injected, each persisted event is also handed
  to it so the trade's live Telegram card is posted or refreshed for every
  subscribed owner. When a ``SignalBroadcastService`` is injected, the event is recorded
  against the signal's broadcast cycle too, so the public channel message shows
  which workers executed the signal and where each of them stands. Both are
  best-effort and never block persistence.
  """

  def __init__(
    self,
    trade_repository: TradeRepository,
    connection: NatsClient | None = None,
    card_service: "TradeCardService | None" = None,
    signal_broadcast_service: "SignalBroadcastService | None" = None,
  ) -> None:
    self._repo = trade_repository
    self._conn = connection or nats_client
    self._cards = card_service
    self._signal_broadcast = signal_broadcast_service
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

    if self._cards is not None:
      try:
        await self._cards.handle_event(event, trade)
      except Exception as exc:
        # Card delivery must never break TRADE consumption.
        log.exception("Failed to queue trade card update: %s", exc)

    if self._signal_broadcast is not None:
      try:
        await self._signal_broadcast.record_execution(event, trade)
      except Exception as exc:
        # Same rule: the execution table is a nicety on top of the TRADE row.
        log.exception("Failed to record execution on the broadcast cycle: %s", exc)


class SystemEventConsumer:
  """Consumes SYSTEM events from NATS and answers the WORKER_CONNECTED handshake.

  Answers with a single WORKER_CONNECTED_ACK carrying the worker's whole initial
  configuration (strategy magic map, retry replay, the account's own settings,
  and the crypto leverage block for crypto workers), because a reply inbox only
  accepts one message.
  """

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
    # The strategy_magic_map is now read on every WORKER_CONNECTED that
    # announces strategies (both markets), so it gets the same short-TTL cache
    # as the crypto settings to absorb reconnect-storm bursts.
    self._magic_map_cache: dict[str, int] | None = None
    self._magic_map_cached_at: float = 0.0

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
    back to broadcasting the same answer on the SYSTEM subject.
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

    # A reply inbox accepts exactly one message, so the whole answer is
    # assembled here and sent as a single ACK: the crypto leverage config
    # (crypto workers only), the magic map (mandatory for every market), and
    # the replay of recent signals so a reconnecting worker catches up on what
    # it missed while offline.
    crypto_leverage: Optional[CryptoLeverageConfig] = None
    if event.market == MarketEnum.CRYPTO.value:
      # Resolved first so a misconfigured crypto worker is rejected before we
      # spend a signals query on a handshake that cannot be answered.
      crypto_leverage = await self._build_crypto_leverage(event.account_id, reply_to)
      if crypto_leverage is None:
        # Settings are missing or invalid; _build_crypto_leverage already
        # replied with the reason. Never follow that with an ACK — a crypto
        # worker must not be told it is configured when it is not.
        return

    magic_map = await self._build_strategy_magic_map(event.strategies)
    retry_signals = await self._build_retry_signals(event.account_id, event.strategies)
    account_settings = await self._build_account_settings(event)

    await self._reply_ack(
      reply_to,
      event.account_id,
      strategy_magic_map=magic_map,
      retry_signals=retry_signals,
      settings=account_settings,
      crypto_leverage_init=crypto_leverage,
    )

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

  async def _build_account_settings(
    self, event: SystemWorkerConnectedSignal
  ) -> AccountSettings:
    """Return what the account's owner set from the bot (``/prevent`` & co.).

    Read straight from the row on every handshake — deliberately *not* cached
    like the broker-wide settings: this one is per account, so a cache would
    only ever help a worker that reconnects twice in a row, and would be worth
    a stale block the moment a user runs a command mid-storm.

    Not cached also means not fatal: a failed read logs and hands the worker
    the schema defaults, same as an account that has never run a command.
    """
    account_id = decompose_worker_id(event.account_id, event.market, event.gateway)
    try:
      raw = await self._accounts.get_settings(
        account_id=account_id,
        market=MarketTypeEnum(event.market),
        gateway=event.gateway,
      )
    except Exception as exc:
      log.exception(
        "SYSTEM settings lookup failed account_id=%s: %s — using defaults",
        account_id,
        exc,
      )
      return AccountSettings()
    return _parse_account_settings(raw, account_id)

  async def _build_strategy_magic_map(self, strategies: list[str]) -> dict[str, int]:
    """Return the strategy → magic-number map filtered to *strategies*.

    Mandatory for every market, so an empty map is a valid result rather than a
    reason to omit the block — the worker announced no known strategy, or the
    setting is unset/invalid.
    """
    announced = set(strategies)
    # No announced strategies → nothing could match anyway, so skip the DB read.
    if not announced:
      return {}
    magic_map = await self._get_strategy_magic_map()
    return {k: v for k, v in magic_map.items() if k in announced}

  async def _build_crypto_leverage(
    self, account_id: str, reply_to: str = ""
  ) -> Optional[CryptoLeverageConfig]:
    """Load the crypto settings into a :class:`CryptoLeverageConfig`.

    Returns ``None`` when the settings are missing or invalid, having already
    sent the worker a ``WORKER_CONNECTED_ERROR`` carrying the reason (a no-op
    without a reply inbox) so a requesting worker is not left waiting.
    """
    symbols_raw, leverage_raw = await self._get_crypto_settings()

    if symbols_raw is None or leverage_raw is None:
      log.warning(
        "SYSTEM handshake rejected account_id=%s: "
        "missing crypto settings (%s=%r, %s=%r)",
        account_id,
        CRYPTO_ALLOWED_SYMBOL_KEY,
        symbols_raw,
        CRYPTO_MAX_LEVERAGE_KEY,
        leverage_raw,
      )
      await self._reply_error(reply_to, account_id, "crypto settings not configured")
      return None

    symbols = [s.strip() for s in symbols_raw.split(",") if s.strip()]
    try:
      default_leverage = int(leverage_raw)
    except ValueError:
      log.error(
        "SYSTEM handshake rejected account_id=%s: %s is not an int: %r",
        account_id,
        CRYPTO_MAX_LEVERAGE_KEY,
        leverage_raw,
      )
      await self._reply_error(
        reply_to, account_id, f"{CRYPTO_MAX_LEVERAGE_KEY} is not an integer"
      )
      return None

    if default_leverage <= 0:
      log.error(
        "SYSTEM handshake rejected account_id=%s: %s must be positive, got %r",
        account_id,
        CRYPTO_MAX_LEVERAGE_KEY,
        leverage_raw,
      )
      await self._reply_error(
        reply_to, account_id, f"{CRYPTO_MAX_LEVERAGE_KEY} must be a positive integer"
      )
      return None

    return CryptoLeverageConfig(symbols=symbols, default_leverage=default_leverage)

  async def _build_retry_signals(
    self, account_id: str, strategies: list[str]
  ) -> list[TradingSignal]:
    """Return the replay of the last ``max_retry_timeout`` seconds of signals.

    Empty when the worker announced no strategies, or when we have no
    ``SignalRepository`` wired in (the deployment opted out of the replay).
    Query hits are shaped through ``parse_signal`` so the payload matches the
    live SIGNAL messages exactly. Best-effort: a broken lookup or an invalid
    persisted row is logged and the handshake continues with what could be
    read — a missed replay must not cost the worker its whole configuration.
    """
    if self._signals is None or not strategies:
      return []

    window_seconds = await self._get_max_retry_timeout_seconds()
    try:
      envelopes = await self._signals.list_recent_by_strategies(
        strategies=strategies, since_seconds=window_seconds
      )
    except Exception as exc:
      log.exception(
        "SYSTEM signal replay skipped account_id=%s: signals lookup failed: %s",
        account_id,
        exc,
      )
      return []

    signals: list[TradingSignal] = []
    for envelope in envelopes:
      raw_payload = envelope.get("payload")
      signal_id = envelope.get("signal_id")
      if not isinstance(raw_payload, dict) or not signal_id:
        continue
      try:
        payload = WebhookPayload(**raw_payload)
        # Replaying with the persisted row id is what makes the replay
        # recognisable: the worker sees the same signal_id it saw live and
        # drops the duplicate. The cycle id rides along from the payload.
        signals.append(parse_signal(payload, signal_id))
      except Exception as exc:
        # A single bad row must not derail the replay for the rest.
        log.warning(
          "SYSTEM signal replay skipping bad row signal_id=%s: %s", signal_id, exc
        )

    return signals

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

  async def _get_strategy_magic_map(self) -> dict[str, int]:
    """Return the parsed strategy → magic-number map, reusing a cached read for
    up to ``CRYPTO_SETTINGS_CACHE_TTL_SECONDS`` (shared with the crypto cache).

    The ``strategy_magic_map`` setting is stored as JSON text; parsing happens
    once here and the result is cached, so a reconnect storm re-reads the DB at
    most once per TTL and an admin edit reaches new handshakes within the TTL.
    Both hits and misses (empty map) are cached.
    """
    now = time.monotonic()
    if (
      self._magic_map_cache is not None
      and now - self._magic_map_cached_at < CRYPTO_SETTINGS_CACHE_TTL_SECONDS
    ):
      return self._magic_map_cache

    self._magic_map_cache = _parse_strategy_magic_map(
      await self._settings.get(STRATEGY_MAGIC_MAP_KEY)
    )
    self._magic_map_cached_at = now
    return self._magic_map_cache

  async def _reply_ack(
    self,
    reply_to: str,
    account_id: str,
    *,
    strategy_magic_map: dict[str, int],
    retry_signals: list[TradingSignal],
    settings: AccountSettings,
    crypto_leverage_init: Optional[CryptoLeverageConfig] = None,
  ) -> None:
    """Answer the handshake with the worker's complete initial configuration.

    Delivered on *reply_to* (the request's inbox) so only the worker that asked
    receives it; without a reply inbox it falls back to a broadcast on the
    shared SYSTEM subject that workers filter by ``account_id``.
    """
    try:
      await self._publisher.publish_system_ack(
        subject=reply_to or None,
        account_id=account_id,
        strategy_magic_map=strategy_magic_map,
        retry_signals=retry_signals,
        settings=settings,
        crypto_leverage_init=crypto_leverage_init,
      )
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
    self,
    *,
    signal_id: str,
    strategy: str,
    envelope: dict,
    timeout: float | None = None,
    msg_id: str | None = None,
  ) -> None:
    """Persist a raw webhook envelope to JetStream so it can be handled offline.

    The webhook HTTP path calls this to move the entire signal-handling pipeline
    (parse → publish to workers → notify → mark PUBLISHED) into a background
    consumer. TradingView therefore gets its 202 back as soon as the message is
    durably queued, closing the ``server closed the connection unexpectedly``
    failure mode that came from doing the whole pipeline inline.

    *timeout* bounds the wait for the PubAck (nats-py's own default is 5s —
    longer than TradingView waits for the whole request), and *msg_id* is sent
    as ``Nats-Msg-Id`` so JetStream drops a re-enqueue of an envelope whose
    first ack was merely slow instead of storing the alert twice.

    Raises ``ConnectionError`` when the client has no live connection: nats-py
    would otherwise buffer the write and let the caller wait out the full
    timeout for an ack that cannot arrive.
    """
    if not self._conn.is_connected:
      raise ConnectionError("NATS connection is not established")

    subject = _jetstream_subject(strategy)
    payload = json.dumps(envelope, default=str).encode()
    headers = {api.Header.MSG_ID.value: msg_id} if msg_id else None
    ack = await self._conn.js.publish(
      subject, payload, timeout=timeout, headers=headers
    )
    log.info(
      "Enqueued [%s] signal_id=%s msg_id=%s stream_seq=%s duplicate=%s",
      subject,
      signal_id,
      msg_id,
      getattr(ack, "seq", None),
      getattr(ack, "duplicate", False),
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
    signal_uxid: str | None = None,
  ) -> None:
    """Broadcast a FLAT (close-all) directive on the strategy subject.

    Carries both ids the LONG/SHORT/TP payloads (a full ``TradingSignal``)
    carry, and for the same reasons: ``signal_id`` is unique per signal, so a
    worker seeing this directive live and then again inside a
    WORKER_CONNECTED_ACK's ``retry_signals`` de-duplicates by id instead of
    guessing on content; ``signal_uxid`` names the trade cycle being closed.
    """
    payload = json.dumps(
      {
        "signal_id": signal_id,
        "signal_uxid": signal_uxid,
        "strategy": strategy,
        "timestamp": timestamp.isoformat(),
        "action": SignalActionEnum.FLAT.value,
        "symbol": symbol,
      }
    ).encode()
    await self._conn.nc.publish(strategy, payload)
    log.info(
      "Published [%s] FLAT directive signal_id=%s signal_uxid=%s symbol=%s",
      strategy,
      signal_id,
      signal_uxid,
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
      subject = compose_admin_subject(signal.market, signal.gateway, signal.account_id)
    else:
      subject = PublishTopicEnum.ADMIN.value
    payload = signal.model_dump_json().encode()
    await self._conn.nc.publish(subject, payload)
    log.info(
      "Published [%s] action=%s strategy=%s symbol=%s account_id=%s market=%s "
      "gateway=%s ref_id=%s",
      subject,
      signal.action,
      signal.strategy,
      signal.symbol,
      signal.account_id,
      signal.market,
      signal.gateway,
      signal.ref_id,
    )

  async def publish_system_signal(
    self, *, subject: str | None = None, **kwargs
  ) -> None:
    """Publish a standalone CRYPTO_LEVERAGE_INIT system signal.

    Used by the ``POST /admin/settings/crypto-*`` push to reach workers that are
    already connected; a worker's connect-time copy travels inside its
    WORKER_CONNECTED_ACK instead. When *subject* is given the signal is
    delivered to that one worker; otherwise it is broadcast on the shared SYSTEM
    subject.
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

  async def publish_system_ack(self, *, subject: str | None = None, **kwargs) -> None:
    """Answer a WORKER_CONNECTED handshake with the worker's whole initial
    configuration — magic map, signal replay, account settings and (crypto
    only) leverage config — in the one message a NATS reply inbox accepts.

    Delivered on *subject* (the request's reply inbox) when set, so only the
    worker that asked sees its own configuration; otherwise broadcast on the
    shared SYSTEM subject for a fire-and-forget worker to filter by
    ``account_id``.
    """
    signal = SystemWorkerConnectedAck(**kwargs)
    target = subject or PublishTopicEnum.SYSTEM.value
    body = signal.model_dump_json()
    await self._conn.nc.publish(target, body.encode())
    # The whole handshake answer now lives in this one message, so the payload
    # goes into the log with it: when a worker starts up wrong, this line is
    # the record of exactly what it was told. One per connect, so the volume is
    # bounded by reconnects rather than by traffic.
    log.info(
      "Published [SYSTEM→%s] action=%s account_id=%s strategies=%d retry_signals=%d "
      "crypto_leverage=%s | payload=%s",
      target,
      signal.action,
      signal.account_id,
      len(signal.strategy_magic_map),
      len(signal.retry_signals),
      signal.crypto_leverage_init is not None,
      body,
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
