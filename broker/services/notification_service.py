"""
broker/services/notification_service.py — Notification channels.

``send_message`` is async and uses an httpx.AsyncClient so that sending a
Telegram message never blocks the event loop (previously a synchronous
``requests.post`` stalled the whole webhook handler for up to its timeout).

This module also owns the Telegram **error-log hook**. A standard
:class:`logging.Handler` cannot ``await`` anything: ``emit`` is synchronous and
may run from any context (sync code, the event loop, a worker thread). Yet
:class:`TelegramNotification` sends over the network with ``httpx`` and must be
awaited. :class:`TelegramLogHandler` bridges the two with a queue + background
worker:

* ``emit`` only formats the record and hands it to the event loop via
  ``loop.call_soon_threadsafe`` — it never blocks and never raises.
* A long-lived worker task (started during the app lifespan) drains the queue
  and performs the actual async send via :class:`TelegramLogNotification`.

Two safeguards keep this from misbehaving in production:

* **No recursion.** This module logs an error when a Telegram send fails. A
  filter drops records originating from it, so a failing send can never trigger
  another send.
* **No spam.** Identical messages are suppressed within a short dedup window and
  the queue is bounded, dropping records when saturated rather than growing
  without limit.
"""

from __future__ import annotations

import abc
import asyncio
import logging
import time

import httpx

from broker.constants import SILENT_SIGNAL
from broker.helpers import emoji_constants as em
from broker.interfaces.db_protocol import SettingRepository
from broker.logger import get_logger
from broker.settings import settings

logger = get_logger("broker.services.notification_service")

_HTTP_TIMEOUT = 5.0


def _box(text: str) -> str:
  return f"<pre>{text.strip()}</pre>"


class Notification(abc.ABC):
  """Base class for Telegram notification channels.

  Owns everything the channels share: the enabled flag, the credentials, the
  Bot API ``url`` built from the token, and the send itself (payload, HTTP
  call, error logging). Subclasses only customise *which* credentials they
  default to, how the body is formatted (:meth:`format_text`) and whether a
  send should be skipped (:meth:`should_send`)."""

  #: Name of the setting a subclass reads its token from, for warning messages.
  token_setting_name = "TELEGRAM_BOT_TOKEN"

  def __init__(self, chat_id: str | None = None, bot_token: str | None = None):
    self.enabled = settings.telegram.ENABLED
    self.bot_token = bot_token
    self.chat_id = chat_id

  @property
  def url(self) -> str:
    """Bot API sendMessage endpoint for this channel's token."""
    return f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

  def format_text(self, message_text: str) -> str:
    """Hook: transform the body just before sending. Default is as-is."""
    return message_text

  async def should_send(self) -> bool:
    """Hook: per-send veto, checked after the enabled/credential guards."""
    return True

  async def send_message(self, message_text: str, chat_id: str | None = None) -> bool:
    """Deliver *message_text* (HTML) to *chat_id*, or to the channel's own
    ``chat_id`` when omitted. Returns True on a 200 send, False on any
    skip/failure — never raises, since notifications are best-effort."""
    if not self.enabled:
      logger.debug("Telegram notifications are disabled in settings.")
      return False

    target = chat_id if chat_id is not None else self.chat_id
    if not self.bot_token or not target:
      logger.warning(
        "%s and a chat id must be set for notifications.", self.token_setting_name
      )
      return False

    if not await self.should_send():
      return False

    payload = {
      "chat_id": target,
      "text": self.format_text(message_text),
      "parse_mode": "HTML",
    }

    try:
      async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        response = await client.post(self.url, json=payload)
      if response.status_code != 200:
        logger.error(
          "Failed to send Telegram message chat_id=%s: %s", target, response.text
        )
        return False
      return True
    except Exception as exc:
      logger.exception(
        "Exception sending Telegram message chat_id=%s: %s", target, exc
      )
      return False


class TelegramNotification(Notification):
  """Sends HTML-formatted messages to a Telegram chat via the Bot API. Silently
  no-ops when disabled or misconfigured."""

  def __init__(
    self,
    chat_id: str | None = None,
    bot_token: str | None = None,
    setting_repository: SettingRepository | None = None,
  ):
    super().__init__(
      chat_id=chat_id if chat_id is not None else settings.telegram.CHAT_ID,
      bot_token=bot_token if bot_token is not None else settings.telegram.BOT_TOKEN,
    )
    self._setting_repository = setting_repository

  def format_text(self, message_text: str) -> str:
    return _box(message_text)

  async def should_send(self) -> bool:
    if self._setting_repository is None:
      return True
    silent = await self._setting_repository.get(SILENT_SIGNAL)
    if silent == "1":
      logger.debug("SILENT_SIGNAL is enabled; skipping notification.")
      return False
    return True


class OwnerBroadcastNotifier(Notification):
  """Sends a Telegram DM to a specific chat id via the bot-service bot token.

  Unlike :class:`TelegramNotification` (one fixed chat, wraps every message in a
  ``<pre>`` box), this targets an arbitrary ``chat_id`` per call and sends the
  HTML body as-is — completed-trade broadcasts carry their own ``<b>`` markup.

  The token defaults to ``BOT_TELEGRAM_TOKEN`` (the bot users actually DM),
  not the broker's own notification bot: a user can only be messaged by the bot
  they started. Silently no-ops when Telegram is disabled or the token is
  unset, so a deployment that doesn't share the bot token simply never
  broadcasts."""

  token_setting_name = "BOT_TELEGRAM_TOKEN"

  def __init__(self, bot_token: str | None = None) -> None:
    super().__init__(
      bot_token=bot_token if bot_token is not None else settings.telegram.SERVICE_BOT_TOKEN
    )


# ── Telegram error-log hook ────────────────────────────────────────────────

# Loggers whose records must never be forwarded, to avoid an infinite
# send → fail → log error → send loop. Both the notification path and the log
# handler below live under this module's logger name.
_EXCLUDED_PREFIXES = ("broker.services.notification_service",)

_QUEUE_MAXSIZE = 100


class TelegramLogNotification(TelegramNotification):
  """Telegram channel dedicated to forwarded error logs.

  Targets the private log chat/bot when ``TELEGRAM_LOG_*`` is configured, and
  otherwise falls back to the shared management chat/bot."""

  def __init__(self) -> None:
    super().__init__(
      chat_id=settings.telegram.LOG_CHAT_ID or settings.telegram.CHAT_ID,
      bot_token=settings.telegram.LOG_BOT_TOKEN or settings.telegram.BOT_TOKEN,
    )


class _RecursionFilter(logging.Filter):
  """Drop records emitted by the Telegram send path itself."""

  def filter(self, record: logging.LogRecord) -> bool:
    return not record.name.startswith(_EXCLUDED_PREFIXES)


class TelegramLogHandler(logging.Handler):
  """Logging handler that forwards ERROR+ records to the management chat."""

  def __init__(self) -> None:
    super().__init__(level=logging.ERROR)
    self.addFilter(_RecursionFilter())
    self.setFormatter(
      logging.Formatter(
        fmt="[BROKER]\n%(levelname)s | %(name)s\n%(message)s",
      )
    )
    self._loop: asyncio.AbstractEventLoop | None = None
    self._queue: asyncio.Queue[str] | None = None
    self._task: asyncio.Task[None] | None = None
    # message -> monotonic timestamp of last forward, for dedup.
    self._recent: dict[str, float] = {}

  # ── lifecycle (called from the app lifespan) ─────────────────────
  def start(self, loop: asyncio.AbstractEventLoop) -> None:
    """Bind the running event loop and launch the background worker."""
    self._loop = loop
    if self._queue is None:
      self._queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
    if self._task is None or self._task.done():
      self._task = loop.create_task(self._worker())

  async def stop(self) -> None:
    """Cancel the worker task cleanly."""
    if self._task is not None:
      self._task.cancel()
      try:
        await self._task
      except asyncio.CancelledError:
        pass
      self._task = None

  # ── logging.Handler API ──────────────────────────────────────────
  def emit(self, record: logging.LogRecord) -> None:
    try:
      loop = self._loop
      if loop is None or loop.is_closed():
        return  # not started yet (or shut down) — nothing to forward to

      message = self.format(record)
      if self._is_duplicate(message):
        return

      loop.call_soon_threadsafe(self._enqueue, message)
    except Exception:  # pragma: no cover — handlers must never raise
      self.handleError(record)

  # ── internals ────────────────────────────────────────────────────
  def _is_duplicate(self, message: str) -> bool:
    """Return True if *message* was forwarded within the dedup window."""
    window = settings.telegram.LOG_DEDUP_WINDOW
    now = time.monotonic()
    # Prune stale entries so the dict cannot grow unbounded.
    self._recent = {msg: ts for msg, ts in self._recent.items() if now - ts < window}
    if message in self._recent:
      return True
    self._recent[message] = now
    return False

  def _enqueue(self, message: str) -> None:
    """Push onto the queue from within the event loop thread; drop if full."""
    if self._queue is None:
      return
    try:
      self._queue.put_nowait(message)
    except asyncio.QueueFull:
      pass  # under an error storm, dropping is preferable to blocking

  async def _worker(self) -> None:
    assert self._queue is not None
    notifier = TelegramLogNotification()
    while True:
      message = await self._queue.get()
      try:
        await notifier.send_message(f"{em.ERROR_ALERT} {message}")
      except Exception:
        # Never surface failures back through logging (would risk recursion)
        # and never let the worker die.
        pass
      finally:
        self._queue.task_done()


# Shared singleton: every logger forwards through one queue/worker.
telegram_log_handler = TelegramLogHandler()
