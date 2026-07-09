from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from broker.schemas.core import MarketEnum, SignalActionEnum


class PublishTopicEnum(str, Enum):
  """NATS subjects the broker publishes to or listens on for system-level and trading messages."""

  SIGNAL = "SIGNAL"
  ADMIN = "ADMIN"
  TRADE = "TRADE"
  SYSTEM = "SYSTEM"


class AdminActionEnum(str, Enum):
  """Admin actions that can be published to the ADMIN topic."""

  FLAT = "FLAT"


class SystemActionEnum(str, Enum):
  """System actions exchanged on the SYSTEM topic between broker and workers."""

  # Outgoing (broker → worker)
  CRYPTO_LEVERAGE_INIT = "CRYPTO_LEVERAGE_INIT"

  # Outgoing reply (broker → worker): acknowledges a WORKER_CONNECTED handshake
  # that needs no further configuration (e.g. a non-crypto worker).
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

  signal_id: str
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
  """Admin signal published to the ADMIN topic."""

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
      }
    },
  )

  action: AdminActionEnum
  timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
  strategy: Optional[str] = None
  symbol: Optional[str] = None
  account_id: Optional[str] = None


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


class SystemCryptoLeverageInitSignal(SystemSignal):
  """Outbound CRYPTO_LEVERAGE_INIT signal the broker pushes to a crypto worker.

  ``symbols`` (allowed crypto symbols) and ``default_leverage`` (max leverage)
  are loaded from BrokerSetting so the worker can apply that configuration.
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
  initial configuration to push back.
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
      }
    },
  )

  action: SystemActionEnum = SystemActionEnum.WORKER_CONNECTED
  market: MarketEnum = Field(..., description="Market the worker serves.")
  gateway: str = Field(..., description="Gateway/broker the worker uses.")


class SystemWorkerConnectedAck(SystemSignal):
  """Broker → worker reply confirming a WORKER_CONNECTED handshake that needs no
  further configuration (e.g. a non-crypto worker).

  Sent on the request's reply inbox so a worker that used NATS ``request`` gets a
  definitive answer instead of timing out.
  """

  model_config = ConfigDict(
    use_enum_values=True,
    json_schema_extra={
      "example": {
        "action": "WORKER_CONNECTED_ACK",
        "account_id": "FOREX-MT5-12345678",
        "timestamp": "2026-06-30T00:00:00+00:00",
      }
    },
  )

  action: SystemActionEnum = SystemActionEnum.WORKER_CONNECTED_ACK


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
