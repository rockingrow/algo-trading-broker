"""
broker/providers.py — FastAPI dependency providers.

This is the composition root for the HTTP layer: it wires concrete
implementations to the abstractions that routers and services depend on, so
those modules never import infrastructure directly.
"""

from __future__ import annotations

from fastapi import Depends, Request

from broker.db.repository import (
  SqlAlchemyAccountRepository,
  SqlAlchemyBroadcastMessageRepository,
  SqlAlchemySettingRepository,
  SqlAlchemySignalRepository,
  SqlAlchemyTradeBroadcastRepository,
  SqlAlchemyTradeRepository,
)
from broker.interfaces import (
  AccountRepository,
  BroadcastMessageRepository,
  Notifier,
  SettingRepository,
  SignalBroadcaster,
  SignalPublisher,
  SignalRepository,
  TradeBroadcastRepository,
  TradeRepository,
)
from broker.services.broadcast_service import (
  BroadcastDispatcher,
  SignalBroadcastService,
)
from broker.services.notification_service import TelegramNotification
from broker.services.signal_processing_service import (
  DeferredEnqueuer,
  SignalProcessingService,
)
from broker.settings import settings


def get_signal_repository() -> SignalRepository:
  return SqlAlchemySignalRepository()


def get_setting_repository() -> SettingRepository:
  return SqlAlchemySettingRepository()


def get_account_repository() -> AccountRepository:
  return SqlAlchemyAccountRepository()


def get_trade_repository() -> TradeRepository:
  return SqlAlchemyTradeRepository()


def get_trade_broadcast_repository() -> TradeBroadcastRepository:
  return SqlAlchemyTradeBroadcastRepository()


def get_broadcast_message_repository() -> BroadcastMessageRepository:
  return SqlAlchemyBroadcastMessageRepository()


def make_signal_broadcaster() -> SignalBroadcastService:
  """Build the signal-cycle broadcast *writer* outside a FastAPI request.

  The HTTP layer (via ``get_signal_broadcaster``), the JetStream signal worker
  and the TRADE consumer all record onto the same cycles; a plain factory
  keeps the wiring consistent and lets non-request contexts (app lifespan)
  reuse it without going through ``Depends``.

  It only writes — Telegram delivery is the :class:`BroadcastDispatcher`'s
  job, driven off the write log this records into.
  """
  return SignalBroadcastService(
    repository=get_broadcast_message_repository(),
    signal_repository=get_signal_repository(),
  )


def get_signal_broadcaster() -> SignalBroadcaster:
  """Broadcast recorder for trade signals (private + public chats)."""
  return make_signal_broadcaster()


def make_broadcast_dispatcher(
  setting_repository: SettingRepository,
) -> BroadcastDispatcher:
  """CDC dispatcher that turns write-log entries into Telegram edits.

  One per process, started and stopped with the app: it owns a Postgres
  LISTEN connection plus a sweeper task that re-reads the log on a timer.
  """
  return BroadcastDispatcher(
    repository=get_broadcast_message_repository(),
    setting_repository=setting_repository,
  )


def make_signals_notifier(setting_repository: SettingRepository) -> Notifier:
  """Build the trade/signal notification channel outside a FastAPI request.

  Both the HTTP layer (via ``get_signals_notifier``) and the JetStream signal
  worker need this same channel; a plain factory keeps the wiring consistent
  and lets non-request contexts (app lifespan) reuse it without going through
  ``Depends``.
  """
  return TelegramNotification(
    chat_id=settings.telegram.BROKER_CHANNEL_CHAT_IDS or settings.telegram.BROKER_LOG_CHAT_IDS,
    setting_repository=setting_repository,
  )


def get_signals_notifier(
  setting_repository: SettingRepository = Depends(get_setting_repository),
) -> Notifier:
  """Channel for trade/signal notifications (falls back to the management chat)."""
  return make_signals_notifier(setting_repository)


def get_admin_notifier() -> Notifier:
  """Channel for management/admin notifications."""
  return TelegramNotification(chat_id=settings.telegram.BROKER_LOG_CHAT_IDS)


def get_publisher(request: Request) -> SignalPublisher:
  """The NATS publisher created during app startup and stored on app.state."""
  return request.app.state.publisher


def get_deferred_enqueuer(request: Request) -> DeferredEnqueuer | None:
  """The background enqueue-retry queue created during app startup.

  Request-scoped like every other dependency here, but the object itself
  outlives the request — the whole point is that it keeps retrying a webhook's
  JetStream publish after that webhook has been answered. ``None`` when the app
  was assembled without a lifespan (tests), which simply means a failed enqueue
  is reported instead of retried.
  """
  return getattr(request.app.state, "deferred_enqueuer", None)


def get_signal_service(
  signal_repository: SignalRepository = Depends(get_signal_repository),
  setting_repository: SettingRepository = Depends(get_setting_repository),
  publisher: SignalPublisher = Depends(get_publisher),
  notifier: Notifier = Depends(get_signals_notifier),
  broadcaster: SignalBroadcaster = Depends(get_signal_broadcaster),
  deferred_enqueuer: DeferredEnqueuer | None = Depends(get_deferred_enqueuer),
) -> SignalProcessingService:
  return SignalProcessingService(
    signal_repository=signal_repository,
    setting_repository=setting_repository,
    publisher=publisher,
    notifier=notifier,
    broadcaster=broadcaster,
    webhook_secret=settings.webhook.SECRET,
    deferred_enqueuer=deferred_enqueuer,
  )
