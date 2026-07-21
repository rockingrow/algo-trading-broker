import httpx

from broker.services import notification_service as ns
from broker.services.notification_service import TelegramNotification, _box


class FakeSettingRepo:
  def __init__(self, value=None):
    self.value = value

  async def get(self, key):
    return self.value

  async def set(self, key, value):
    return True


def test_box_wraps_and_strips():
  assert _box("  hello  ") == "<pre>hello</pre>"


async def test_disabled_is_noop(monkeypatch):
  monkeypatch.setattr(ns.settings.telegram, "ENABLED", False)
  sent = []
  monkeypatch.setattr(httpx, "AsyncClient", _client_recorder(sent))

  notifier = TelegramNotification(chat_id="c")
  await notifier.send_message("hi")
  assert sent == []


async def test_silent_signal_skips_send(monkeypatch):
  monkeypatch.setattr(ns.settings.telegram, "ENABLED", True)
  monkeypatch.setattr(ns.settings.telegram, "BOT_TOKEN", "tok")
  sent = []
  monkeypatch.setattr(httpx, "AsyncClient", _client_recorder(sent))

  notifier = TelegramNotification(
    chat_id="c", setting_repository=FakeSettingRepo(value="1")
  )
  await notifier.send_message("hi")
  assert sent == []


async def test_missing_token_or_chat_id_is_noop(monkeypatch):
  monkeypatch.setattr(ns.settings.telegram, "ENABLED", True)
  monkeypatch.setattr(ns.settings.telegram, "BOT_TOKEN", "")
  sent = []
  monkeypatch.setattr(httpx, "AsyncClient", _client_recorder(sent))

  notifier = TelegramNotification(chat_id="c")
  await notifier.send_message("hi")
  assert sent == []


async def test_happy_path_posts_to_telegram(monkeypatch):
  monkeypatch.setattr(ns.settings.telegram, "ENABLED", True)
  monkeypatch.setattr(ns.settings.telegram, "BOT_TOKEN", "tok")
  sent = []
  monkeypatch.setattr(httpx, "AsyncClient", _client_recorder(sent, status_code=200))

  notifier = TelegramNotification(
    chat_id="chat-123", setting_repository=FakeSettingRepo(value="0")
  )
  await notifier.send_message("hello world")

  assert len(sent) == 1
  url, payload = sent[0]
  assert url == "https://api.telegram.org/bottok/sendMessage"
  assert payload["chat_id"] == "chat-123"
  assert payload["parse_mode"] == "HTML"
  assert payload["text"] == "<pre>hello world</pre>"


async def test_non_200_is_handled_gracefully(monkeypatch):
  monkeypatch.setattr(ns.settings.telegram, "ENABLED", True)
  monkeypatch.setattr(ns.settings.telegram, "BOT_TOKEN", "tok")
  sent = []
  monkeypatch.setattr(httpx, "AsyncClient", _client_recorder(sent, status_code=400))

  notifier = TelegramNotification(chat_id="c")
  # Should not raise despite a 400 response.
  await notifier.send_message("hi")
  assert len(sent) == 1


async def test_network_exception_is_swallowed(monkeypatch):
  monkeypatch.setattr(ns.settings.telegram, "ENABLED", True)
  monkeypatch.setattr(ns.settings.telegram, "BOT_TOKEN", "tok")

  class BoomClient:
    def __init__(self, *a, **k):
      pass

    async def __aenter__(self):
      return self

    async def __aexit__(self, *a):
      return False

    async def post(self, url, json):
      raise httpx.ConnectError("down")

  monkeypatch.setattr(httpx, "AsyncClient", BoomClient)

  notifier = TelegramNotification(chat_id="c")
  # Exception must be caught inside send_message.
  await notifier.send_message("hi")


# ── helpers ─────────────────────────────────────────────────────────


def _client_recorder(sink: list, status_code: int = 200):
  """Build a fake httpx.AsyncClient class that records POST calls into *sink*."""

  class _Resp:
    def __init__(self):
      self.status_code = status_code
      self.text = "err" if status_code != 200 else "ok"

  class _Client:
    def __init__(self, *a, **k):
      pass

    async def __aenter__(self):
      return self

    async def __aexit__(self, *a):
      return False

    async def post(self, url, json):
      sink.append((url, json))
      return _Resp()

  return _Client
