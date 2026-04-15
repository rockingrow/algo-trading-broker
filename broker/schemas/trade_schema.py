from enum import Enum


class TradeStatusEnum(str, Enum):
  OPENED = "OPENED"
  REJECTED = "REJECTED"
  PARTIALLY_CLOSED = "PARTIALLY_CLOSED"
  CLOSED = "CLOSED"
