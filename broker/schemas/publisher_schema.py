from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from broker.schemas.account_schema import AccountSettings, MarketTypeEnum
from broker.schemas.core import MarketEnum, SignalActionEnum


class PublishTopicEnum(str, Enum):
  """NATS subjects the broker publishes to or listens on for system-level and trading messages."""

  SIGNAL = "SIGNAL"
  ADMIN = "ADMIN"
  TRADE = "TRADE"
  SYSTEM = "SYSTEM"


def compose_admin_subject(market, gateway: str, account_id: str) -> str:
  """Build the per-account private ADMIN subject
  ``ADMIN.<market>.<gateway>.<account_id>`` (e.g. ``ADMIN.FOREX.MT5.12345678``).

  Account-scoped admin actions are published here instead of on the shared
  ``ADMIN`` subject so that only the one worker subscribed to this exact
  subject receives them. No other worker sees the ``account_id``, keeping each
  worker isolated to its own account.

  ``market`` may be a :class:`MarketTypeEnum` (or any enum with a string
  ``value``) or its bare string value; both render to the bare market name.
  """
  market = market.value if isinstance(market, Enum) else str(market)
  return f"{PublishTopicEnum.ADMIN.value}.{market}.{gateway}.{account_id}"


class AdminActionEnum(str, Enum):
  """Admin actions that can be published to the ADMIN topic."""

  FLAT = "FLAT"
  # Block / allow new signals for a scope (strategy/symbol/account). Workers
  # must honor these to take effect — enforcement lives in the worker code.
  BLOCK_SIGNAL = "BLOCK_SIGNAL"
  ALLOW_SIGNAL = "ALLOW_SIGNAL"


class SystemActionEnum(str, Enum):
  """System actions exchanged on the SYSTEM topic between broker and workers."""

  # Outgoing (broker → worker): pushed on its own only when an admin edits the
  # crypto settings of an already-connected worker. The connect-time copy rides
  # inside WORKER_CONNECTED_ACK instead.
  CRYPTO_LEVERAGE_INIT = "CRYPTO_LEVERAGE_INIT"

  # Outgoing reply (broker → worker): the one and only answer to a
  # WORKER_CONNECTED handshake, carrying the worker's entire initial
  # configuration (see :class:`SystemWorkerConnectedAck`).
  WORKER_CONNECTED_ACK = "WORKER_CONNECTED_ACK"

  # Outgoing reply (broker → worker): the handshake was received but the broker
  # could not build the initial configuration; the worker may surface the reason
  # and/or retry.
  WORKER_CONNECTED_ERROR = "WORKER_CONNECTED_ERROR"

  # Incoming (worker → broker): published by a worker right after it connects
  # to NATS to announce its presence and request initial configuration.
  WORKER_CONNECTED = "WORKER_CONNECTED"


class ScalingSchema(BaseModel):
  """Scaling block carrying the target levels and size used when scaling an existing position."""

  tp: Optional[float] = None
  sl: Optional[float] = None
  quantity: Optional[float] = None


class TradingSignal(BaseModel):
  """Normalised signal produced from a TradingView webhook payload.

  Published on the strategy subject for workers to act on. ``action`` selects
  the trade operation (entry, partial-close target, stop-loss, flatten) while
  ``symbol``, ``price`` and ``quantity`` describe the instrument and size. The
  optional fields carry stop-loss / take-profit levels, risk sizing and the
  ``scaling`` block used when adding to an existing position.
  """

  model_config = ConfigDict(use_enum_values=True)

  # Identity of THIS signal — the broker's ``signals`` row id, minted per
  # persisted signal and therefore unique per action. This is the
  # de-duplication key: a worker that sees a signal live and then again inside
  # a ``retry_signals`` replay recognises it by this id alone.
  signal_id: str
  # Identity of the trade **cycle** the signal belongs to — the ``signal_uxid``
  # from the webhook payload, shared by the entry and every TP/SL/FLAT that
  # follows it. It is for correlation, never for de-duplication: a worker uses
  # it to tie a close back to the position it opened. Optional, because a
  # payload that predates the field has none.
  signal_uxid: Optional[str] = None
  timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

  strategy: str
  action: SignalActionEnum
  symbol: str = Field(..., description="Instrument symbol, e.g. XAUUSD")
  price: float
  quantity: float
  sl: Optional[float] = None
  tp1: Optional[float] = None
  tp2: Optional[float] = None
  tp1_percent: Optional[float] = None
  move_sl_to_be: Optional[bool] = None
  is_running: Optional[bool] = None
  risk_percent: Optional[float] = None
  is_scale_position: Optional[bool] = None
  scale_strategy: Optional[str] = None
  scaling: Optional[ScalingSchema] = None


class AdminSignal(BaseModel):
  """Admin signal published to workers.

  Routing depends on scope:

  * **Account-scoped** (``account_id`` set) — published to the per-account
    private subject ``ADMIN.<market>.<gateway>.<account_id>`` (see
    :func:`compose_admin_subject`). Only the single worker subscribed to that
    subject receives it, so no other worker ever learns the ``account_id`` and
    each worker stays isolated to its own account. ``market``/``gateway`` are
    REQUIRED whenever ``account_id`` is set (see
    ``uq_accounts_market_gateway_account_id`` on the ``accounts`` table) so the
    subject is fully disambiguated — the same bare id can exist under different
    market/gateway pairs.
  * **Broadcast** (no ``account_id``) — published to the shared ``ADMIN``
    subject and fanned out to every connected worker, which filters for itself
    (e.g. a strategy/symbol-scoped or flat-everything directive).
  """

  model_config = ConfigDict(
    use_enum_values=True,
    from_attributes=True,
    json_schema_extra={
      "example": {
        "action": "FLAT",
        "timestamp": "2026-06-02T08:00:00+00:00",
        "strategy": "my_strategy",
        "symbol": "XAUUSD",
        "account_id": "123456",
        "market": "FOREX",
        "gateway": "MT5",
      }
    },
  )

  action: AdminActionEnum
  timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
  strategy: Optional[str] = None
  symbol: Optional[str] = None
  account_id: Optional[str] = None
  market: Optional[MarketTypeEnum] = None
  gateway: Optional[str] = None

  @model_validator(mode="after")
  def _require_market_gateway_with_account_id(self) -> "AdminSignal":
    if self.account_id is not None and (self.market is None or self.gateway is None):
      raise ValueError("market and gateway are required when account_id is set")
    return self


class SystemSignal(BaseModel):
  """Base for signals exchanged on the SYSTEM topic between broker and workers.

  Holds the fields common to every SYSTEM message; concrete actions subclass
  this and add their own payload. ``account_id`` carries the worker identifier
  in the ``<market>-<gateway>-<account_id>`` format (e.g.
  ``FOREX-MT5-12345678``, ``CRYPTO-BINANCE-7654321``).
  """

  model_config = ConfigDict(use_enum_values=True)

  action: SystemActionEnum
  account_id: str = Field(
    ...,
    description="Worker identifier in the format <market>-<gateway>-<account_id>.",
  )
  timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CryptoLeverageConfig(BaseModel):
  """Allowed crypto symbols + default leverage, as loaded from BrokerSetting.

  Nested inside :class:`SystemWorkerConnectedAck` for a crypto worker's connect
  handshake. The standalone :class:`SystemCryptoLeverageInitSignal` carries the
  same two values for the admin push to already-connected workers.
  """

  model_config = ConfigDict(
    json_schema_extra={
      "example": {
        "symbols": ["BTC", "ETH"],
        "default_leverage": 10,
      }
    },
  )

  symbols: list[str] = Field(
    default_factory=list, description="Crypto symbols the worker may trade."
  )
  default_leverage: int = Field(..., description="Max leverage the worker applies.")


class SystemCryptoLeverageInitSignal(SystemSignal):
  """Outbound CRYPTO_LEVERAGE_INIT signal the broker pushes to a crypto worker.

  ``symbols`` (allowed crypto symbols) and ``default_leverage`` (max leverage)
  are loaded from BrokerSetting so the worker can apply that configuration.

  Only used for the *live* push from ``POST /admin/settings/crypto-*``, which
  reaches workers that are already connected. A worker's connect-time copy
  arrives in the ``crypto_leverage_init`` block of its WORKER_CONNECTED_ACK.
  """

  model_config = ConfigDict(
    use_enum_values=True,
    json_schema_extra={
      "example": {
        "action": "CRYPTO_LEVERAGE_INIT",
        "account_id": "CRYPTO-BINANCE-7654321",
        "timestamp": "2026-06-30T00:00:00+00:00",
        "symbols": ["BTC", "ETH"],
        "default_leverage": 10,
      }
    },
  )

  action: SystemActionEnum = SystemActionEnum.CRYPTO_LEVERAGE_INIT
  symbols: Optional[list[str]] = None
  default_leverage: Optional[int] = None


class SystemWorkerConnectedSignal(SystemSignal):
  """Inbound WORKER_CONNECTED event a worker publishes on connect (worker → broker).

  ``account_id``, ``market`` and ``gateway`` are all required so the broker knows
  which worker connected and which market/gateway it serves before deciding what
  initial configuration to push back. ``strategies`` lists the strategy subjects
  the worker subscribes to — the broker uses them to select both the
  ``strategy_magic_map`` entries and the ``retry_signals`` replay it answers
  with.
  """

  model_config = ConfigDict(
    use_enum_values=True,
    json_schema_extra={
      "example": {
        "action": "WORKER_CONNECTED",
        "account_id": "CRYPTO-BINANCE-7654321",
        "timestamp": "2026-06-30T00:00:00+00:00",
        "market": "CRYPTO",
        "gateway": "BINANCE",
        "strategies": ["wt_cross_v1", "MT5_GOLD_M5_V1"],
      }
    },
  )

  action: SystemActionEnum = SystemActionEnum.WORKER_CONNECTED
  market: MarketEnum = Field(..., description="Market the worker serves.")
  gateway: str = Field(..., description="Gateway/broker the worker uses.")
  strategies: list[str] = Field(
    default_factory=list,
    description=(
      "Strategy subjects the worker subscribes to. Drives the reply: only "
      "these strategies' magic numbers and signals are sent back."
    ),
  )


class SystemWorkerConnectedAck(SystemSignal):
  """Broker → worker reply confirming a WORKER_CONNECTED handshake, carrying the
  worker's complete initial configuration.

  This is the **single** message the broker sends back, because a NATS reply
  inbox only ever accepts one: ``request()`` resolves its future (or, in the
  ``old_style`` form, auto-unsubscribes at ``max_msgs=1``) on the first reply
  and silently drops the rest. Everything the handshake used to send as separate
  messages therefore travels inside this one payload:

  * ``strategy_magic_map`` — strategy → magic number, from the
    ``strategy_magic_map`` BrokerSetting, filtered to the strategies the worker
    announced. Always present; ``{}`` means nothing matched.
  * ``retry_signals`` — every SIGNAL persisted in the last ``max_retry_timeout``
    seconds for those same strategies, shaped exactly like the live payloads on
    the strategy subject so the worker can replay them through the same handler
    and de-duplicate by ``signal_id``. Always present; ``[]`` means nothing to
    replay.
  * ``settings`` — the worker's own ``accounts.settings`` blob: what its owner
    set from the bot (e.g. ``signal_blocked`` via /prevent). Always present and
    always complete — an account that has never run a command gets the schema
    defaults — so a worker starting up, or reconnecting after being offline,
    applies the owner's current state instead of its own defaults.
  * ``crypto_leverage_init`` — allowed symbols + default leverage, **only** for
    a crypto worker; ``None`` for every other market.

  Sent on the request's reply inbox so a worker that used NATS ``request`` gets a
  definitive answer instead of timing out, and so no other worker sees this
  worker's configuration. A fire-and-forget worker (no reply inbox) gets the
  same payload broadcast on the shared SYSTEM subject and filters by
  ``account_id``.
  """

  model_config = ConfigDict(
    use_enum_values=True,
    json_schema_extra={
      "example": {
        "action": "WORKER_CONNECTED_ACK",
        "account_id": "FOREX-MT5-12345678",
        "timestamp": "2026-06-30T00:00:00+00:00",
        "strategy_magic_map": {
          "MT5_GOLD_M5_V1": 20260409,
          "MT5_MULTI_M5_V1": 20260708,
        },
        "retry_signals": [
          {
            "signal_id": "sig_123",
            "signal_uxid": "9f2c4b7e18a3d605",
            "timestamp": "2026-06-29T23:59:30+00:00",
            "strategy": "MT5_GOLD_M5_V1",
            "action": "LONG",
            "symbol": "XAUUSD",
            "price": 2350.5,
            "quantity": 0.1,
            "sl": 2340.0,
            "tp1": 2370.0,
            "tp2": 2390.0,
            "risk_percent": 1.0,
          }
        ],
        "settings": {"signal_blocked": False},
        "crypto_leverage_init": None,
      }
    },
  )

  action: SystemActionEnum = SystemActionEnum.WORKER_CONNECTED_ACK
  strategy_magic_map: dict[str, int] = Field(
    default_factory=dict,
    description=(
      "Strategy name → magic number, filtered to the strategies the worker "
      "announced on WORKER_CONNECTED."
    ),
  )
  retry_signals: list[TradingSignal] = Field(
    default_factory=list,
    description=(
      "Signals persisted in the last ``max_retry_timeout`` seconds whose "
      "strategy the worker announced. Same shape as the SIGNAL payload."
    ),
  )
  settings: AccountSettings = Field(
    default_factory=AccountSettings,
    description=(
      "The worker's per-account settings, as set from the bot (e.g. "
      "``signal_blocked`` from /prevent). Always complete: an account that "
      "has never run a command gets the defaults."
    ),
  )
  crypto_leverage_init: Optional[CryptoLeverageConfig] = Field(
    default=None,
    description=(
      "Allowed symbols + default leverage for a crypto worker; None for any "
      "other market."
    ),
  )


class SystemWorkerConnectedError(SystemSignal):
  """Broker → worker reply signalling the handshake was received but the broker
  could not build the initial configuration (missing/invalid settings, an
  unreadable payload, …).

  Sent on the request's reply inbox so the worker can surface ``reason`` and/or
  retry. ``account_id`` is optional because a payload may be too malformed to
  identify the worker.
  """

  model_config = ConfigDict(
    use_enum_values=True,
    json_schema_extra={
      "example": {
        "action": "WORKER_CONNECTED_ERROR",
        "account_id": "CRYPTO-BINANCE-7654321",
        "timestamp": "2026-06-30T00:00:00+00:00",
        "reason": "crypto settings not configured",
      }
    },
  )

  action: SystemActionEnum = SystemActionEnum.WORKER_CONNECTED_ERROR
  account_id: Optional[str] = None
  reason: str = Field(..., description="Human-readable failure reason.")
