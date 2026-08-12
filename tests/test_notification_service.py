import asyncio

import httpx

from broker.services import notification_service as ns
from broker.services.notification_service import (
  ChatTarget,
  QueuedNotifier,
  TelegramNotification,
  _box,
  parse_chat_targets,
)


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


# ── chat targets: multiple groups & forum topics ────────────────────


def test_parse_chat_targets_keeps_a_plain_chat_id_untouched():
  assert parse_chat_targets("-1001111111111") == [ChatTarget("-1001111111111")]


def test_parse_chat_targets_splits_a_topic_suffix():
  # "<chat id>_<topic id>" is what a group with Topics enabled looks like.
  assert parse_chat_targets("-1002173777783_924584") == [
    ChatTarget("-1002173777783", 924584)
  ]


def test_parse_chat_targets_reads_a_comma_separated_list():
  raw = " -1001111111111 ,-1002173777783_924584,, @public_channel "
  assert parse_chat_targets(raw) == [
    ChatTarget("-1001111111111"),
    ChatTarget("-1002173777783", 924584),
    ChatTarget("@public_channel"),
  ]


def test_parse_chat_targets_collapses_duplicates():
  raw = "-100111,-100111,-100111_5"
  assert parse_chat_targets(raw) == [ChatTarget("-100111"), ChatTarget("-100111", 5)]


def test_parse_chat_targets_ignores_empty_values():
  assert parse_chat_targets("") == []
  assert parse_chat_targets(None) == []
  assert parse_chat_targets(" , ") == []


def test_parse_chat_targets_does_not_invent_topics_from_usernames():
  # A username may legitimately contain underscores (and end in digits); only a
  # numeric chat id can carry a topic suffix.
  assert parse_chat_targets("@my_group_2") == [ChatTarget("@my_group_2")]
  assert parse_chat_targets("-100111_abc") == [ChatTarget("-100111_abc")]


async def test_topic_chat_id_sends_message_thread_id(monkeypatch):
  monkeypatch.setattr(ns.settings.telegram, "ENABLED", True)
  monkeypatch.setattr(ns.settings.telegram, "BOT_TOKEN", "tok")
  sent = []
  monkeypatch.setattr(httpx, "AsyncClient", _client_recorder(sent))

  notifier = TelegramNotification(chat_id="-1002173777783_924584")
  assert await notifier.send_message("hi") is True

  assert len(sent) == 1
  _, payload = sent[0]
  assert payload["chat_id"] == "-1002173777783"
  # Bot API type is Integer, and the topic must not leak into the chat id.
  assert payload["message_thread_id"] == 924584


async def test_plain_chat_id_omits_message_thread_id(monkeypatch):
  monkeypatch.setattr(ns.settings.telegram, "ENABLED", True)
  monkeypatch.setattr(ns.settings.telegram, "BOT_TOKEN", "tok")
  sent = []
  monkeypatch.setattr(httpx, "AsyncClient", _client_recorder(sent))

  notifier = TelegramNotification(chat_id="-1001111111111")
  await notifier.send_message("hi")

  # Telegram rejects the field on a group without that thread, so it may only
  # be present when a topic was actually configured.
  assert "message_thread_id" not in sent[0][1]


async def test_fans_out_to_every_configured_chat(monkeypatch):
  monkeypatch.setattr(ns.settings.telegram, "ENABLED", True)
  monkeypatch.setattr(ns.settings.telegram, "BOT_TOKEN", "tok")
  sent = []
  monkeypatch.setattr(httpx, "AsyncClient", _client_recorder(sent))

  notifier = TelegramNotification(chat_id="-100111,-1002173777783_924584,@chan")
  assert await notifier.send_message("hello") is True

  assert [(p["chat_id"], p.get("message_thread_id")) for _, p in sent] == [
    ("-100111", None),
    ("-1002173777783", 924584),
    ("@chan", None),
  ]
  # One body, formatted once, delivered to each.
  assert {p["text"] for _, p in sent} == {"<pre>hello</pre>"}


async def test_one_failing_chat_does_not_stop_the_others(monkeypatch):
  monkeypatch.setattr(ns.settings.telegram, "ENABLED", True)
  monkeypatch.setattr(ns.settings.telegram, "BOT_TOKEN", "tok")
  sent = []
  # Middle group answers 400 (bot kicked, topic closed, …).
  monkeypatch.setattr(
    httpx,
    "AsyncClient",
    _client_recorder(
      sent, status_for=lambda p: 400 if p["chat_id"] == "-100222" else 200
    ),
  )

  notifier = TelegramNotification(chat_id="-100111,-100222,-100333")
  # Reported as a failure, but every other group was still notified.
  assert await notifier.send_message("hi") is False
  assert [p["chat_id"] for _, p in sent] == ["-100111", "-100222", "-100333"]


async def test_chat_id_that_parses_to_nothing_is_a_noop(monkeypatch):
  monkeypatch.setattr(ns.settings.telegram, "ENABLED", True)
  monkeypatch.setattr(ns.settings.telegram, "BOT_TOKEN", "tok")
  sent = []
  monkeypatch.setattr(httpx, "AsyncClient", _client_recorder(sent))

  notifier = TelegramNotification(chat_id=" , ")
  assert await notifier.send_message("hi") is False
  assert sent == []


# ── every chat-id *setting* takes a list, not just the signals channel ──
#
# The parsing lives in one place (``Notification.send_message``), but each
# setting reaches it by a different route — a default argument, a provider, a
# subclass with its own fallback — so each route is pinned here.


def _targets_of(sent):
  """(chat_id, message_thread_id) actually posted, in order."""
  return [(p["chat_id"], p.get("message_thread_id")) for _, p in sent]


async def test_management_chat_id_setting_takes_a_list(monkeypatch):
  """TELEGRAM_CHAT_ID — broker lifecycle + admin notifications."""
  monkeypatch.setattr(ns.settings.telegram, "ENABLED", True)
  monkeypatch.setattr(ns.settings.telegram, "BOT_TOKEN", "tok")
  monkeypatch.setattr(ns.settings.telegram, "CHAT_ID", "-100111,-1002173777783_924584")
  sent = []
  monkeypatch.setattr(httpx, "AsyncClient", _client_recorder(sent))

  # No chat_id argument: the channel falls back to the setting.
  await TelegramNotification().send_message("hi")

  assert _targets_of(sent) == [("-100111", None), ("-1002173777783", 924584)]


async def test_signals_channel_setting_takes_a_list(monkeypatch):
  """TELEGRAM_CHAT_CHANNEL_ID — published trade alerts, via the provider."""
  from broker.providers import make_signals_notifier

  monkeypatch.setattr(ns.settings.telegram, "ENABLED", True)
  monkeypatch.setattr(ns.settings.telegram, "BOT_TOKEN", "tok")
  monkeypatch.setattr(
    ns.settings.telegram, "CHAT_CHANNEL_ID", "-100111,-1002173777783_924584"
  )
  sent = []
  monkeypatch.setattr(httpx, "AsyncClient", _client_recorder(sent))

  notifier = make_signals_notifier(FakeSettingRepo(value="0"))
  await notifier.send_message("signal")

  assert _targets_of(sent) == [("-100111", None), ("-1002173777783", 924584)]


async def test_log_chat_id_setting_takes_a_list(monkeypatch):
  """TELEGRAM_LOG_CHAT_ID — forwarded ERROR logs."""
  monkeypatch.setattr(ns.settings.telegram, "ENABLED", True)
  monkeypatch.setattr(ns.settings.telegram, "BOT_TOKEN", "tok")
  monkeypatch.setattr(ns.settings.telegram, "LOG_BOT_TOKEN", "")
  monkeypatch.setattr(
    ns.settings.telegram, "LOG_CHAT_ID", "-100999,-1002173777783_924584"
  )
  sent = []
  monkeypatch.setattr(httpx, "AsyncClient", _client_recorder(sent))

  await ns.TelegramLogNotification().send_message("boom")

  assert _targets_of(sent) == [("-100999", None), ("-1002173777783", 924584)]


async def test_log_chat_id_falls_back_to_the_management_list(monkeypatch):
  """An unset TELEGRAM_LOG_CHAT_ID inherits TELEGRAM_CHAT_ID — list included."""
  monkeypatch.setattr(ns.settings.telegram, "ENABLED", True)
  monkeypatch.setattr(ns.settings.telegram, "BOT_TOKEN", "tok")
  monkeypatch.setattr(ns.settings.telegram, "LOG_BOT_TOKEN", "")
  monkeypatch.setattr(ns.settings.telegram, "LOG_CHAT_ID", "")
  monkeypatch.setattr(ns.settings.telegram, "CHAT_ID", "-100111,-100222_7")
  sent = []
  monkeypatch.setattr(httpx, "AsyncClient", _client_recorder(sent))

  await ns.TelegramLogNotification().send_message("boom")

  assert _targets_of(sent) == [("-100111", None), ("-100222", 7)]


async def test_owner_broadcast_chat_id_takes_a_list(monkeypatch):
  """Per-call chat ids (owner DMs) go through the same parsing."""
  monkeypatch.setattr(ns.settings.telegram, "ENABLED", True)
  sent = []
  monkeypatch.setattr(httpx, "AsyncClient", _client_recorder(sent))

  notifier = ns.OwnerBroadcastNotifier(bot_token="svc-tok")
  await notifier.send_message("closed", chat_id="555,777_3")

  assert _targets_of(sent) == [("555", None), ("777", 3)]


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


# ── QueuedNotifier ──────────────────────────────────────────────────


class SlowNotifier:
  """Stands in for a send to a throttled api.telegram.org."""

  def __init__(self, delay: float = 0.2):
    self.delay = delay
    self.sent: list[str] = []
    self.started = asyncio.Event()

  async def send_message(self, message_text, chat_id=None):
    self.started.set()
    await asyncio.sleep(self.delay)
    self.sent.append(message_text)
    return True


async def test_queued_notifier_returns_before_the_send_completes():
  inner = SlowNotifier(delay=0.2)
  notifier = QueuedNotifier(inner)
  await notifier.start()
  try:
    started = asyncio.get_running_loop().time()
    await notifier.send_message("signal")
    elapsed = asyncio.get_running_loop().time() - started

    # The caller — the JetStream fan-out — must not inherit Telegram's latency.
    assert elapsed < 0.1
    assert inner.sent == []
    await asyncio.wait_for(notifier._queue.join(), timeout=2)
    assert inner.sent == ["signal"]
  finally:
    await notifier.stop()


async def test_queued_notifier_preserves_order():
  inner = SlowNotifier(delay=0.01)
  notifier = QueuedNotifier(inner)
  await notifier.start()
  try:
    for text in ("first", "second", "third"):
      await notifier.send_message(text)
    await asyncio.wait_for(notifier._queue.join(), timeout=2)
  finally:
    await notifier.stop()

  assert inner.sent == ["first", "second", "third"]


async def test_queued_notifier_drops_when_the_backlog_is_full():
  inner = SlowNotifier(delay=5)
  notifier = QueuedNotifier(inner, maxsize=1)

  # Not started: nothing drains, so the second message has nowhere to go and is
  # dropped rather than blocking the pipeline it was queued to stay out of.
  assert await notifier.send_message("kept") is True
  assert await notifier.send_message("dropped") is False
  assert notifier.pending == 1


async def test_queued_notifier_survives_a_failing_send():
  class BoomNotifier:
    def __init__(self):
      self.calls = 0

    async def send_message(self, message_text, chat_id=None):
      self.calls += 1
      raise RuntimeError("telegram down")

  inner = BoomNotifier()
  notifier = QueuedNotifier(inner)
  await notifier.start()
  try:
    await notifier.send_message("one")
    await notifier.send_message("two")
    await asyncio.wait_for(notifier._queue.join(), timeout=2)
  finally:
    await notifier.stop()

  # A failed send must not take the drain task down with it.
  assert inner.calls == 2


# ── helpers ─────────────────────────────────────────────────────────


def _client_recorder(sink: list, status_code: int = 200, status_for=None):
  """Build a fake httpx.AsyncClient class that records POST calls into *sink*.

  ``status_for`` optionally derives the status code from the payload, so a test
  can fail one chat of a fan-out while the rest succeed."""

  class _Resp:
    def __init__(self, code):
      self.status_code = code
      self.text = "err" if code != 200 else "ok"

  class _Client:
    def __init__(self, *a, **k):
      pass

    async def __aenter__(self):
      return self

    async def __aexit__(self, *a):
      return False

    async def post(self, url, json):
      sink.append((url, json))
      return _Resp(status_for(json) if status_for else status_code)

  return _Client
