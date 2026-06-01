"""
broker/services/notification_service.py — Notification channels.

``send_message`` is async and uses an httpx.AsyncClient so that sending a
Telegram message never blocks the event loop (previously a synchronous
``requests.post`` stalled the whole webhook handler for up to its timeout).
"""

from __future__ import annotations

import abc

import httpx

from broker.constants import SILENT_SIGNAL
from broker.interfaces.db_protocol import SettingRepository
from broker.logger import get_logger
from broker.settings import settings

logger = get_logger("broker.services.notification_service")

_HTTP_TIMEOUT = 5.0


def _box(text: str) -> str:
  return f"<pre>{text.strip()}</pre>"


class Notification(abc.ABC):
  """Abstract base class defining the interface for notification channels."""

  @abc.abstractmethod
  async def send_message(self, message_text: str) -> None:
    """Deliver *message_text* to the channel."""
    raise NotImplementedError


class TelegramNotification(Notification):
  """Sends HTML-formatted messages to a Telegram chat via the Bot API. Silently
  no-ops when disabled or misconfigured."""

  def __init__(
    self,
    chat_id: str | None = None,
    setting_repository: SettingRepository | None = None,
  ):
    self.enabled = settings.TELEGRAM_ENABLED
    self.bot_token = settings.TELEGRAM_BOT_TOKEN
    self.chat_id = chat_id if chat_id is not None else settings.TELEGRAM_CHAT_ID
    self._setting_repository = setting_repository

  async def send_message(self, message_text: str) -> None:
    if not self.enabled:
      logger.debug("Telegram notifications are disabled in settings.")
      return

    if self._setting_repository is not None:
      silent = await self._setting_repository.get(SILENT_SIGNAL)
      if silent == "1":
        logger.debug("SILENT_SIGNAL is enabled; skipping notification.")
        return

    if not self.bot_token or not self.chat_id:
      logger.warning(
        "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set for notifications."
      )
      return

    url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
    payload = {
      "chat_id": self.chat_id,
      "text": _box(message_text),
      "parse_mode": "HTML",
    }

    try:
      async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        response = await client.post(url, json=payload)
      if response.status_code != 200:
        logger.error("Failed to send Telegram message: %s", response.text)
    except Exception as exc:
      logger.exception("Exception sending Telegram message: %s", exc)
