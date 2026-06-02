from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from broker.schemas.publisher_schema import TradingSignal


@runtime_checkable
class SignalPublisher(Protocol):
  """Publishes trading signals / directives to downstream subscribers."""

  async def publish(self, signal: TradingSignal) -> None: ...

  async def publish_flat(
    self, symbol: str, timestamp: datetime, strategy: str
  ) -> None: ...

  async def publish_admin_signal(self, **kwargs) -> None: ...
