from enum import Enum


class SignalActionEnum(str, Enum):
  """All possible trade actions carried by a signal: entries, partial-close targets, stop-loss, and full flatten."""

  LONG = "LONG"
  SHORT = "SHORT"
  TP1 = "TP1"
  TP2 = "TP2"
  R_SL = "R_SL"
  SL = "SL"
  FLAT = "FLAT"


class MarketEnum(str, Enum):
  CRYPTO = "CRYPTO"
  FOREX = "FOREX"


class ForexGatewayEnum(str, Enum):
  MT5 = "MT5"


class CryptoGatewayEnum(str, Enum):
  BINANCE = "BINANCE"


class SignalStatusEnum(str, Enum):
  """Delivery state of a persisted webhook signal.

  ``QUEUED`` — the signal was written to the DB and enqueued on JetStream but
  the background handler has not yet fanned it out to workers.

  ``PUBLISHED`` — the JetStream handler successfully published the signal to
  the strategy subject (and ran the notification pipeline).
  """

  QUEUED = "QUEUED"
  PUBLISHED = "PUBLISHED"
