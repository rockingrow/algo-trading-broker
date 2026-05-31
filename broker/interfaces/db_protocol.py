from __future__ import annotations

from typing import Protocol, runtime_checkable

from broker.db.models import Account, Trade
from broker.schemas.trade_event_schema import PositionEvent
from broker.schemas.webhook_schema import WebhookPayload


@runtime_checkable
class SignalRepository(Protocol):
  """Persists inbound TradingView webhook signals."""

  async def log_signal(self, payload: WebhookPayload) -> str | None: ...


@runtime_checkable
class SettingRepository(Protocol):
  """Reads and writes broker-level key/value settings."""

  async def get(self, key: str) -> str | None: ...

  async def set(self, key: str, value: str) -> bool: ...


@runtime_checkable
class AccountRepository(Protocol):
  """Reads trading accounts known to the broker."""

  async def get_all(self) -> list[Account]: ...


@runtime_checkable
class TradeRepository(Protocol):
  """Applies position events from workers to the broker's trades table."""

  async def upsert_by_position_event(self, event: PositionEvent) -> Trade | None: ...
