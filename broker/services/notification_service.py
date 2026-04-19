import requests

from broker.logger import get_logger
from broker.settings import settings

logger = get_logger("broker.services.notification_service")


def _box(text: str) -> str:
  return f"<pre>{text.strip()}</pre>"


class Notification:
  def send_message(self, message_text: str):
    raise NotImplementedError("This method must be implemented by a subclass")


class TelegramNotification(Notification):
  def __init__(self, chat_id: str | None = None):
    self.enabled = settings.TELEGRAM_ENABLED
    self.bot_token = settings.TELEGRAM_BOT_TOKEN
    self.chat_id = chat_id if chat_id is not None else settings.TELEGRAM_CHAT_ID

  def send_message(self, message_text: str):
    if not self.enabled:
      logger.debug("Telegram notifications are disabled in settings.")
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
      response = requests.post(url, json=payload, timeout=5)
      if response.status_code != 200:
        logger.error(f"Failed to send Telegram message: {response.text}")
      return response.json()
    except Exception as e:
      logger.exception(f"Exception sending Telegram message: {e}")
      return None
