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
