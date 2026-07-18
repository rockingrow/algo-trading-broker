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

  ``QUEUED`` — the signal was written to the DB but the background handler
  has not yet successfully fanned it out to workers. May still be retried by
  the retry job as long as ``attempts > 0``.

  ``PUBLISHED`` — the handler successfully published the signal to the
  strategy subject (and ran the notification pipeline).

  ``FAILED`` — every attempt exhausted (``attempts`` decremented to ``0``)
  without a successful publish. Terminal — the retry job stops re-picking it.
  """

  QUEUED = "QUEUED"
  PUBLISHED = "PUBLISHED"
  FAILED = "FAILED"
