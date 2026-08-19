from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from broker.db.models import (
  Account,
  BroadcastMessage,
  BroadcastMessageChat,
  BroadcastMessageLog,
  BroadcastMessageWorker,
  Signal,
  Trade,
)
from broker.schemas.account_schema import AccountLinkSummary, MarketTypeEnum
from broker.schemas.core import (
  BotPlatformTypeEnum,
  BroadcastAudienceEnum,
  SignalActionEnum,
)
from broker.schemas.trade_event_schema import PositionEvent
from broker.schemas.trade_schema import TradeStatusEnum
from broker.schemas.webhook_schema import WebhookPayload


@dataclass(frozen=True)
class BroadcastCycleView:
  """Everything the dispatcher needs to render and deliver one cycle.

  Loaded in a single call so the message body, the chats it goes to and the
  worker table inside it all come from the same database snapshot — rendering
  from three separately-timed reads could show a worker row that the body's
  own state does not know about yet.
  """

  message: BroadcastMessage
  chats: list[BroadcastMessageChat] = field(default_factory=list)
  workers: list[BroadcastMessageWorker] = field(default_factory=list)


@runtime_checkable
class SignalRepository(Protocol):
  """Persists inbound TradingView webhook signals."""

  async def log_signal(self, payload: WebhookPayload) -> str | None: ...

  async def mark_published(self, signal_id: str) -> bool: ...

  async def get_by_id(self, signal_id: str) -> Signal | None: ...

  async def record_attempt_failure(self, signal_id: str) -> Signal | None: ...

  async def list_retryable(self, retry_interval_seconds: int) -> list[Signal]: ...

  async def list_recent_by_strategies(
    self, strategies: list[str], since_seconds: int
  ) -> list[dict]: ...


@runtime_checkable
class SettingRepository(Protocol):
  """Reads and writes broker-level key/value settings."""

  async def get(self, key: str) -> str | None: ...

  async def get_many(self, keys: list[str]) -> dict[str, str]: ...

  async def set(self, key: str, value: str) -> bool: ...


@runtime_checkable
class AccountRepository(Protocol):
  """Reads trading accounts known to the broker, and records the market/gateway
  a worker announces on connect."""

  async def upsert_gateway(
    self, account_id: str, market: MarketTypeEnum, gateway: str
  ) -> None: ...

  async def create_account(
    self,
    account_id: str,
    market: MarketTypeEnum,
    gateway: str,
    account_name: str | None = None,
  ) -> Account | None: ...

  async def get_all(self) -> list[Account]: ...

  async def get_by_market(self, market: MarketTypeEnum) -> list[Account]: ...

  async def get_link_summaries(
    self,
    account_ids: list[uuid.UUID],
    platform: BotPlatformTypeEnum = BotPlatformTypeEnum.TELEGRAM,
  ) -> dict[uuid.UUID, AccountLinkSummary]: ...

  # Bot-user methods take the caller's platform id as ``telegram_user_id: int``
  # because that is what the ``/v1/telegram/*`` endpoints receive. The
  # implementation stores it as a string keyed by ``platform``; see
  # ``AccountBotLink``.
  async def list_by_telegram_user_id(
    self,
    telegram_user_id: int,
    platform: BotPlatformTypeEnum = BotPlatformTypeEnum.TELEGRAM,
  ) -> list[Account]: ...

  async def get_active_account(
    self,
    telegram_user_id: int,
    platform: BotPlatformTypeEnum = BotPlatformTypeEnum.TELEGRAM,
  ) -> Account | None: ...

  async def set_active_account(
    self,
    telegram_user_id: int,
    account_id: uuid.UUID,
    platform: BotPlatformTypeEnum = BotPlatformTypeEnum.TELEGRAM,
  ) -> Account | None: ...

  async def link_telegram(
    self,
    token: uuid.UUID,
    telegram_user_id: int,
    platform: BotPlatformTypeEnum = BotPlatformTypeEnum.TELEGRAM,
  ) -> Account | None: ...

  async def admin_link_telegram(
    self,
    account_uuid: uuid.UUID,
    telegram_user_id: int,
    platform: BotPlatformTypeEnum = BotPlatformTypeEnum.TELEGRAM,
  ) -> Account | None: ...

  async def unlink_telegram(
    self,
    telegram_user_id: int,
    platform: BotPlatformTypeEnum = BotPlatformTypeEnum.TELEGRAM,
  ) -> bool: ...

  async def rotate_link_token(self, account_id: str) -> uuid.UUID | None: ...


@runtime_checkable
class TradeBroadcastRepository(Protocol):
  """Per-user opt-in for completed-trade Telegram broadcasts and target lookup."""

  async def subscribe(
    self,
    telegram_user_id: int,
    platform: BotPlatformTypeEnum = BotPlatformTypeEnum.TELEGRAM,
  ) -> bool: ...

  async def unsubscribe(
    self,
    telegram_user_id: int,
    platform: BotPlatformTypeEnum = BotPlatformTypeEnum.TELEGRAM,
  ) -> bool: ...

  async def is_subscribed(
    self,
    telegram_user_id: int,
    platform: BotPlatformTypeEnum = BotPlatformTypeEnum.TELEGRAM,
  ) -> bool: ...

  async def list_broadcast_targets(
    self,
    account_id: str,
    market: MarketTypeEnum | None,
    gateway: str | None,
    platform: BotPlatformTypeEnum = BotPlatformTypeEnum.TELEGRAM,
  ) -> list[str]: ...


@runtime_checkable
class BroadcastMessageRepository(Protocol):
  """Stores one Telegram message per signal cycle, the per-chat copies of it,
  the workers that executed it, and the write log that drives delivery.

  The split is deliberate: the first two methods are *writers* (a signal, a
  worker report) that only touch the database, and the rest is what the
  dispatcher uses to turn those durable changes into Telegram edits.
  """

  async def record_event(
    self,
    *,
    strategy: str,
    signal_uxid: str,
    symbol: str,
    timeframe: str | None,
    action: SignalActionEnum,
    event: dict,
  ) -> BroadcastMessage | None: ...

  async def record_worker_execution(
    self,
    *,
    strategy: str,
    signal_uxid: str,
    worker_id: str,
    account_id: str,
    market: MarketTypeEnum | None,
    gateway: str | None,
    latest_status: TradeStatusEnum,
    latest_action: str | None = None,
    reject_reason: str | None = None,
    event_at: datetime | None = None,
  ) -> BroadcastMessage | None: ...

  async def claim_pending_logs(
    self, broadcast_message_id: uuid.UUID, *, max_attempts: int
  ) -> list[BroadcastMessageLog]: ...

  async def finish_logs(
    self,
    log_ids: list[uuid.UUID],
    *,
    delivered: bool,
    error: str | None = None,
    max_attempts: int | None = None,
  ) -> bool: ...

  async def list_cycles_with_pending_logs(
    self, *, max_attempts: int, stale_after_seconds: int, limit: int = 50
  ) -> list[uuid.UUID]: ...

  async def reclaim_stale_logs(self, *, stale_after_seconds: int) -> int: ...

  async def load_cycle(
    self, broadcast_message_id: uuid.UUID
  ) -> BroadcastCycleView | None: ...

  async def upsert_chat(
    self,
    broadcast_message_id: uuid.UUID,
    *,
    audience: BroadcastAudienceEnum,
    chat_id: str,
    message_id: str | None,
    message: str | None,
    delivered_seq: int | None = None,
    last_error: str | None = None,
  ) -> bool: ...

  async def mark_broadcast(self, broadcast_message_id: uuid.UUID) -> bool: ...


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

  async def list_open_by_account(self, account_id: str) -> list[Trade]: ...

  async def list_distinct_strategies(self) -> list[str]: ...
