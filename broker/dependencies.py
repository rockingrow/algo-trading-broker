"""
broker/dependencies.py — FastAPI dependency providers.

This is the composition root for the HTTP layer: it wires concrete
implementations to the abstractions that routers and services depend on, so
those modules never import infrastructure directly.
"""

from __future__ import annotations

from fastapi import Depends, Request

from broker.db.repository import (
  SqlAlchemyAccountRepository,
  SqlAlchemySettingRepository,
  SqlAlchemySignalRepository,
)
from broker.interfaces import (
  AccountRepository,
  Notifier,
  SettingRepository,
  SignalPublisher,
  SignalRepository,
)
from broker.services.notification_service import TelegramNotification
from broker.services.signal_processing_service import SignalProcessingService
from broker.settings import settings


def get_signal_repository() -> SignalRepository:
  return SqlAlchemySignalRepository()


def get_setting_repository() -> SettingRepository:
  return SqlAlchemySettingRepository()


def get_account_repository() -> AccountRepository:
  return SqlAlchemyAccountRepository()


def get_signals_notifier() -> Notifier:
  """Channel for trade/signal notifications (falls back to the management chat)."""
  return TelegramNotification(
    chat_id=settings.TELEGRAM_CHAT_CHANNEL_ID or settings.TELEGRAM_CHAT_ID
  )


def get_admin_notifier() -> Notifier:
  """Channel for management/admin notifications."""
  return TelegramNotification(chat_id=settings.TELEGRAM_CHAT_ID)


def get_publisher(request: Request) -> SignalPublisher:
  """The NATS publisher created during app startup and stored on app.state."""
  return request.app.state.publisher


def get_signal_service(
  signal_repository: SignalRepository = Depends(get_signal_repository),
  setting_repository: SettingRepository = Depends(get_setting_repository),
  publisher: SignalPublisher = Depends(get_publisher),
  notifier: Notifier = Depends(get_signals_notifier),
) -> SignalProcessingService:
  return SignalProcessingService(
    signal_repository=signal_repository,
    setting_repository=setting_repository,
    publisher=publisher,
    notifier=notifier,
    webhook_secret=settings.WEBHOOK_SECRET,
  )
