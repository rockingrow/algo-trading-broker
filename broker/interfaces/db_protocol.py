from __future__ import annotations

import uuid
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

  async def get_by_telegram_user_id(self, telegram_user_id: int) -> Account | None: ...

  async def link_telegram(
    self, token: uuid.UUID, telegram_user_id: int
  ) -> Account | None: ...

  async def unlink_telegram(self, telegram_user_id: int) -> bool: ...

  async def rotate_link_token(self, account_id: str) -> uuid.UUID | None: ...


@runtime_checkable
class TradeRepository(Protocol):
  """Applies position events from workers to the broker's trades table."""

  async def upsert_by_position_event(self, event: PositionEvent) -> Trade | None: ...

  async def list_by_account(
    self,
    account_id: str,
    limit: int,
    offset: int,
    order: str = "desc",
    order_by: str = "updatedAt",
  ) -> list[Trade]: ...

  async def count_by_account(self, account_id: str) -> int: ...
