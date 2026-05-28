from enum import Enum


class SubscribeTopicEnum(str, Enum):
  """NATS subjects the broker subscribes to for inbound events from workers."""

  TRADE = "TRADE"
