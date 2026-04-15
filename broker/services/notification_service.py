import requests
from broker.settings import settings


class Notification:
  def send_message(self, message_text: str):
    raise NotImplementedError("This method must be implemented by a subclass")


class TelegramNotification(Notification):
  def __init__(self):
    self.bot_token = settings.TELEGRAM_BOT_TOKEN
    self.chat_id = settings.TELEGRAM_CHAT_ID

  def send_message(self, message_text: str):
    if not self.bot_token or not self.chat_id:
      raise ValueError(
        "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in environment variables."
      )

    url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
    payload = {"chat_id": self.chat_id, "text": message_text, "parse_mode": "Markdown"}

    response = requests.post(url, json=payload)
    return response.json()
