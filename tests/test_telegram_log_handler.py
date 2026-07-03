"""Tests for the Telegram error-log hook (broker.services.notification_service)."""

import asyncio
import logging

import pytest

import broker.services.notification_service as notification_service
from broker.services.notification_service import TelegramLogHandler


@pytest.fixture
def sent(monkeypatch):
  """Capture every message the worker would send to Telegram."""
  messages: list[str] = []

  async def fake_send(self, message_text: str) -> None:
    messages.append(message_text)

  monkeypatch.setattr(
    notification_service.TelegramNotification, "send_message", fake_send
  )
  return messages


async def _settle(handler: TelegramLogHandler) -> None:
  """Let queued ``call_soon_threadsafe`` callbacks run and the worker drain."""
  for _ in range(5):
    await asyncio.sleep(0)
  if handler._queue is not None:
    await handler._queue.join()


def _make_logger(name: str, handler: TelegramLogHandler) -> logging.Logger:
  logger = logging.getLogger(name)
  logger.handlers = [handler]
  logger.setLevel(logging.DEBUG)
  logger.propagate = False
  return logger


async def test_error_is_forwarded(sent):
  handler = TelegramLogHandler()
  handler.start(asyncio.get_running_loop())
  logger = _make_logger("test.telegram.error", handler)

  logger.error("boom %s", 42)
  await _settle(handler)
  await handler.stop()

  assert len(sent) == 1
  assert "boom 42" in sent[0]


async def test_info_is_not_forwarded(sent):
  handler = TelegramLogHandler()
  handler.start(asyncio.get_running_loop())
  logger = _make_logger("test.telegram.info", handler)

  logger.info("just fyi")
  logger.warning("heads up")
  await _settle(handler)
  await handler.stop()

  assert sent == []


async def test_notification_service_logs_do_not_recurse(sent):
  handler = TelegramLogHandler()
  handler.start(asyncio.get_running_loop())
  # A logger living under the notification-service module must be filtered out.
  logger = _make_logger("broker.services.notification_service", handler)

  logger.error("Failed to send Telegram message: 400")
  await _settle(handler)
  await handler.stop()

  assert sent == []


async def test_duplicate_messages_are_deduplicated(sent):
  handler = TelegramLogHandler()
  handler.start(asyncio.get_running_loop())
  logger = _make_logger("test.telegram.dedup", handler)

  logger.error("same error")
  logger.error("same error")
  await _settle(handler)
  await handler.stop()

  assert len(sent) == 1


async def test_worker_uses_dedicated_log_chat_id(monkeypatch):
  """When TELEGRAM_LOG_CHAT_ID is set, the worker must target that chat."""
  from broker.services import notification_service as mod

  monkeypatch.setattr(mod.settings, "TELEGRAM_LOG_CHAT_ID", "999", raising=False)
  monkeypatch.setattr(mod.settings, "TELEGRAM_CHAT_ID", "111", raising=False)

  captured: list[str | None] = []

  original_init = notification_service.TelegramNotification.__init__

  def spy_init(self, chat_id=None, bot_token=None, setting_repository=None):
    captured.append(chat_id)
    original_init(
      self, chat_id=chat_id, bot_token=bot_token, setting_repository=setting_repository
    )

  monkeypatch.setattr(notification_service.TelegramNotification, "__init__", spy_init)

  async def fake_send(self, message_text: str) -> None:
    pass

  monkeypatch.setattr(
    notification_service.TelegramNotification, "send_message", fake_send
  )

  handler = TelegramLogHandler()
  handler.start(asyncio.get_running_loop())
  logger = _make_logger("test.telegram.dedicated", handler)

  logger.error("route me to the private chat")
  await _settle(handler)
  await handler.stop()

  assert "999" in captured  # dedicated chat, not the management fallback


async def test_worker_uses_dedicated_log_bot_token(monkeypatch):
  """When TELEGRAM_LOG_BOT_TOKEN is set, the worker must send via that bot."""
  from broker.services import notification_service as mod

  monkeypatch.setattr(
    mod.settings, "TELEGRAM_LOG_BOT_TOKEN", "log-bot-tok", raising=False
  )
  monkeypatch.setattr(mod.settings, "TELEGRAM_BOT_TOKEN", "main-bot-tok", raising=False)

  captured: list[str | None] = []

  original_init = notification_service.TelegramNotification.__init__

  def spy_init(self, chat_id=None, bot_token=None, setting_repository=None):
    captured.append(bot_token)
    original_init(
      self, chat_id=chat_id, bot_token=bot_token, setting_repository=setting_repository
    )

  monkeypatch.setattr(notification_service.TelegramNotification, "__init__", spy_init)

  async def fake_send(self, message_text: str) -> None:
    pass

  monkeypatch.setattr(
    notification_service.TelegramNotification, "send_message", fake_send
  )

  handler = TelegramLogHandler()
  handler.start(asyncio.get_running_loop())
  logger = _make_logger("test.telegram.dedicated_bot", handler)

  logger.error("send me via the dedicated bot")
  await _settle(handler)
  await handler.stop()

  assert "log-bot-tok" in captured  # dedicated bot, not the shared main bot


async def test_worker_falls_back_to_shared_bot_token(monkeypatch):
  """When TELEGRAM_LOG_BOT_TOKEN is empty, the worker must reuse the shared bot."""
  from broker.services import notification_service as mod

  monkeypatch.setattr(mod.settings, "TELEGRAM_LOG_BOT_TOKEN", "", raising=False)
  monkeypatch.setattr(mod.settings, "TELEGRAM_BOT_TOKEN", "main-bot-tok", raising=False)

  captured: list[str | None] = []

  original_init = notification_service.TelegramNotification.__init__

  def spy_init(self, chat_id=None, bot_token=None, setting_repository=None):
    captured.append(bot_token)
    original_init(
      self, chat_id=chat_id, bot_token=bot_token, setting_repository=setting_repository
    )

  monkeypatch.setattr(notification_service.TelegramNotification, "__init__", spy_init)

  async def fake_send(self, message_text: str) -> None:
    pass

  monkeypatch.setattr(
    notification_service.TelegramNotification, "send_message", fake_send
  )

  handler = TelegramLogHandler()
  handler.start(asyncio.get_running_loop())
  logger = _make_logger("test.telegram.fallback_bot", handler)

  logger.error("send me via the shared bot")
  await _settle(handler)
  await handler.stop()

  assert "main-bot-tok" in captured


async def test_emit_before_start_is_dropped_silently(sent):
  handler = TelegramLogHandler()  # never started — no bound loop
  logger = _make_logger("test.telegram.nostart", handler)

  logger.error("ignored")
  await asyncio.sleep(0)

  assert sent == []
