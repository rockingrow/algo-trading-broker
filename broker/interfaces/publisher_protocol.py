from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from broker.schemas.publisher_schema import TradingSignal


@runtime_checkable
class SignalPublisher(Protocol):
  """Publishes trading signals / directives to downstream subscribers."""

  async def publish_webhook_event(
    self, *, signal_id: str, strategy: str, envelope: dict
  ) -> None: ...

  async def publish(self, signal: TradingSignal) -> None: ...

  async def publish_flat(
    self,
    *,
    signal_id: str,
    symbol: str,
    timestamp: datetime,
    strategy: str,
  ) -> None: ...

  async def publish_admin_signal(self, **kwargs) -> None: ...

  async def publish_system_signal(self, **kwargs) -> None: ...

  async def publish_system_retry_signal(self, **kwargs) -> None: ...

  async def publish_system_ack(self, **kwargs) -> None: ...

  async def publish_system_error(self, **kwargs) -> None: ...
