"""
broker/interfaces.py — Abstractions (Protocols) that decouple high-level
modules from concrete infrastructure (DB, NATS, Telegram).

High-level code (routers, services) depends on these Protocols; concrete
implementations are wired in ``broker/dependencies.py`` and ``broker/app.py``.
This is what makes the application unit-testable with in-memory fakes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from broker.db.models import Account, Trade
from broker.schemas.publisher_schema import TradingSignal
from broker.schemas.trade_event_schema import PositionEvent
from broker.schemas.webhook_schema import WebhookPayload


@runtime_checkable
class Notifier(Protocol):
  """Anything that can deliver a human-readable message to an external channel."""

  async def send_message(self, message_text: str) -> None: ...


@runtime_checkable
class SignalPublisher(Protocol):
  """Publishes trading signals / directives to downstream subscribers."""

  async def publish(self, signal: TradingSignal) -> None: ...

  async def publish_flat(
    self, symbol: str, timestamp: datetime, strategy: str
  ) -> None: ...


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
