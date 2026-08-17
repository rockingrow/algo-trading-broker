"""
broker/services/notification_service.py — Notification channels.

``send_message`` is async and uses an httpx.AsyncClient so that sending a
Telegram message never blocks the event loop (previously a synchronous
``requests.post`` stalled the whole webhook handler for up to its timeout).

Every channel resolves its chat-id setting through :func:`parse_chat_targets`,
so any of them — ``TELEGRAM_CHAT_CHANNEL_ID`` above all — may name several
chats at once and may address a single topic inside a supergroup that has the
Topics feature enabled (``-1002173777783_924584``).

Being non-blocking is not the same as being fast: a send to a throttled
``api.telegram.org`` still *awaits* for the full ``TELEGRAM_HTTP_TIMEOUT``
before raising ``httpx.ReadTimeout``, and any pipeline that awaits it inherits
that delay. :class:`QueuedNotifier` decorates a channel so callers on a latency
budget — the JetStream signal fan-out above all — hand the message off and move
on.

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
import re
import time
from enum import Enum
from typing import Any, NamedTuple

import httpx

from broker.constants import SILENT_SIGNAL
from broker.helpers import emoji_constants as em
from broker.interfaces.db_protocol import SettingRepository
from broker.interfaces.notifier_protocol import Notifier
from broker.logger import get_logger
from broker.settings import settings

logger = get_logger("broker.services.notification_service")


def _box(text: str) -> str:
  return f"<pre>{text.strip()}</pre>"


# ── Chat targets ───────────────────────────────────────────────────────────

# A chat id carrying a forum topic, e.g. "-1002173777783_924584". Only a
# *numeric* chat id may be suffixed this way: a channel username can itself
# contain underscores (``@my_group_2``), so splitting those would invent a
# topic out of part of the name.
_CHAT_TOPIC_RE = re.compile(r"(?P<chat>-?\d+)_(?P<topic>\d+)")


class ChatTarget(NamedTuple):
  """One Telegram destination: a chat, optionally a topic inside it.

  ``message_thread_id`` is the Bot API's name for a forum topic — "unique
  identifier for the target message thread (topic) of a forum; for forum
  supergroups and private chats of bots with forum topic mode enabled only".
  Sending without it lands the message in the group's *General* topic, which
  is why a group with topics enabled needs the id carried all the way down to
  the payload."""

  chat_id: str
  message_thread_id: int | None = None

  @property
  def label(self) -> str:
    """Identify this target in a log line."""
    if self.message_thread_id is None:
      return self.chat_id
    return f"{self.chat_id} (topic {self.message_thread_id})"


def parse_chat_targets(raw: str | None) -> list[ChatTarget]:
  """Parse a chat-id setting into the chats (and topics) to deliver to.

  The value is a comma-separated list, so one channel can fan out to several
  groups: ``"-1001111111111,-1002173777783_924584,@public_channel"``.

  An entry of the form ``<chat id>_<topic id>`` addresses a *topic* inside a
  supergroup that has the Topics feature switched on — the shape Telegram
  itself shows in a topic link (``t.me/c/2173777783/924584``). It is split back
  into the chat and its ``message_thread_id``; everything else is passed
  through untouched, so plain ids (``-1001111111111``), user ids and
  ``@username`` handles keep working exactly as before.

  Blank entries are skipped and duplicates collapse, so a stray comma or a
  chat listed twice costs nothing (and never double-posts).
  """
  if not raw:
    return []

  targets: list[ChatTarget] = []
  for spec in raw.split(","):
    spec = spec.strip()
    if not spec:
      continue
    match = _CHAT_TOPIC_RE.fullmatch(spec)
    target = (
      ChatTarget(match["chat"], int(match["topic"])) if match else ChatTarget(spec)
    )
    if target not in targets:
      targets.append(target)
  return targets


class Notification(abc.ABC):
  """Base class for Telegram notification channels.

  Owns everything the channels share: the enabled flag, the credentials, the
  Bot API ``url`` built from the token, and the send itself (target parsing,
  payload, HTTP call, error logging). Subclasses only customise *which*
  credentials they default to, how the body is formatted (:meth:`format_text`)
  and whether a send should be skipped (:meth:`should_send`)."""

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
    ``chat_id`` when omitted.

    Either may name several chats (comma-separated) and may address a topic
    inside a group — see :func:`parse_chat_targets`. Every chat gets its own
    Bot API call, all in flight together so a slow group costs one
    ``HTTP_TIMEOUT`` for the batch rather than one each.

    Returns True only when every chat took the message; False on any
    skip/failure — never raises, since notifications are best-effort."""
    if not self.enabled:
      logger.debug("Telegram notifications are disabled in settings.")
      return False

    raw_target = chat_id if chat_id is not None else self.chat_id
    if not self.bot_token or not raw_target:
      logger.warning(
        "%s and a chat id must be set for notifications.", self.token_setting_name
      )
      return False

    targets = parse_chat_targets(raw_target)
    if not targets:
      logger.warning("No usable chat id in %r — nothing to notify.", raw_target)
      return False

    if not await self.should_send():
      return False

    text = self.format_text(message_text)
    try:
      async with httpx.AsyncClient(timeout=settings.telegram.HTTP_TIMEOUT) as client:
        results = await asyncio.gather(
          *(self._deliver(client, target, text) for target in targets)
        )
    except Exception as exc:
      logger.exception("Exception sending Telegram message: %s", exc)
      return False
    return all(results)

  async def _deliver(
    self, client: httpx.AsyncClient, target: ChatTarget, text: str
  ) -> bool:
    """POST one already-formatted message to one chat/topic.

    Contains its own failures: with several chats configured, one group the bot
    was kicked from (or one deleted topic) must not stop the others from being
    notified."""
    payload: dict[str, Any] = {
      "chat_id": target.chat_id,
      "text": text,
      "parse_mode": "HTML",
    }
    # Only for topic targets: the Bot API answers "400 Bad Request: message
    # thread not found" when a group has no such thread.
    if target.message_thread_id is not None:
      payload["message_thread_id"] = target.message_thread_id

    try:
      response = await client.post(self.url, json=payload)
    except Exception as exc:
      logger.exception(
        "Exception sending Telegram message chat_id=%s: %s", target.label, exc
      )
      return False

    if response.status_code != 200:
      logger.error(
        "Failed to send Telegram message chat_id=%s: %s", target.label, response.text
      )
      return False
    return True


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
      bot_token=bot_token
      if bot_token is not None
      else settings.telegram.SERVICE_BOT_TOKEN
    )


class EditOutcome(Enum):
  """Result of trying to rewrite an existing broadcast message.

  A tri-state, not a bool, because the caller reacts differently to each: an
  OK edit updates the stored body, a MISSING one falls back to a fresh send
  (the message is gone from the channel and cannot be edited), and a FAILED
  one keeps the stored id and retries on the next pass — re-sending on a rate
  limit would duplicate the cycle in the chat.
  """

  OK = "OK"
  #: The message is gone or can no longer be edited — re-send to recover.
  MISSING = "MISSING"
  #: Transient failure — keep the message id and retry on the next signal.
  FAILED = "FAILED"


#: Bot API error fragments (lower-cased) that mean the message is unrecoverable.
_MESSAGE_GONE_MARKERS = (
  "message to edit not found",
  "message can't be edited",
  "message_id_invalid",
)


class BroadcastNotifier:
  """Sends and edits Telegram messages for one signal-cycle broadcast.

  Distinct from :class:`Notification` because the callers on this side are
  stateful from the message's point of view: one trade cycle owns a single
  message per chat, so the send has to hand back the ``message_id`` for later
  edits, and every subsequent update rewrites that same message in place.
  The body is sent as-is — broadcast bodies carry their own HTML markup rather
  than the ``<pre>`` box :class:`TelegramNotification` wraps every send in.
  """

  def __init__(self, bot_token: str | None = None) -> None:
    self.enabled = settings.telegram.ENABLED
    self.bot_token = bot_token if bot_token is not None else settings.telegram.BOT_TOKEN

  def _api_url(self, method: str) -> str:
    return f"https://api.telegram.org/bot{self.bot_token}/{method}"

  def _ready(self, target: ChatTarget) -> bool:
    if not self.enabled:
      logger.debug("Telegram notifications are disabled in settings.")
      return False
    if not self.bot_token or not target.chat_id:
      logger.warning("TELEGRAM_BOT_TOKEN and a chat id must be set for broadcasts.")
      return False
    return True

  def _payload(self, target: ChatTarget, text: str) -> dict:
    payload: dict[str, Any] = {
      "chat_id": target.chat_id,
      "text": text,
      "parse_mode": "HTML",
      "disable_web_page_preview": True,
    }
    if target.message_thread_id is not None:
      payload["message_thread_id"] = target.message_thread_id
    return payload

  async def send_and_get_message_id(self, target: ChatTarget, text: str) -> str | None:
    """Post *text* to *target* and return Telegram's ``message_id``.

    ``None`` means nothing was sent (disabled/misconfigured) or the send
    failed. A send that succeeded but whose response could not be parsed also
    yields ``None``: without an id the cycle cannot be edited later, so the
    caller must treat it as "no message to update".
    """
    if not self._ready(target):
      return None
    try:
      async with httpx.AsyncClient(timeout=settings.telegram.HTTP_TIMEOUT) as client:
        response = await client.post(
          self._api_url("sendMessage"), json=self._payload(target, text)
        )
      if response.status_code != 200:
        logger.error(
          "Telegram sendMessage failed chat_id=%s: %s",
          target.label,
          response.text,
        )
        return None
      body = _safe_json(response)
      message_id = body.get("message_id") if isinstance(body, dict) else None
      if message_id is None:
        logger.warning(
          "Telegram sendMessage chat_id=%s returned no message_id", target.label
        )
        return None
      return str(message_id)
    except Exception as exc:
      logger.exception(
        "Exception on Telegram sendMessage chat_id=%s: %s", target.label, exc
      )
      return None

  async def edit_message(
    self, target: ChatTarget, message_id: str, text: str
  ) -> EditOutcome:
    """Rewrite an already-sent broadcast message.

    Three outcomes drive different recoveries in the caller. An edit Telegram
    rejects because the body is byte-identical (``message is not modified``)
    counts as OK — it is a no-op, and calling it a failure would make a
    re-delivered signal re-post the whole cycle.
    """
    if not self._ready(target):
      return EditOutcome.FAILED
    payload = self._payload(target, text)
    payload["message_id"] = message_id
    try:
      async with httpx.AsyncClient(timeout=settings.telegram.HTTP_TIMEOUT) as client:
        response = await client.post(self._api_url("editMessageText"), json=payload)
      if response.status_code == 200:
        return EditOutcome.OK
      body = (response.text or "").lower()
      if "message is not modified" in body:
        return EditOutcome.OK
      logger.error(
        "Telegram editMessageText failed chat_id=%s message_id=%s: %s",
        target.label,
        message_id,
        response.text,
      )
      if any(marker in body for marker in _MESSAGE_GONE_MARKERS):
        return EditOutcome.MISSING
      return EditOutcome.FAILED
    except Exception as exc:
      logger.exception(
        "Exception editing Telegram message chat_id=%s message_id=%s: %s",
        target.label,
        message_id,
        exc,
      )
      return EditOutcome.FAILED


def _safe_json(response) -> dict:
  """Best-effort JSON parse of a Bot API response's ``result`` object."""
  try:
    body = response.json()
  except Exception:
    return {}
  result = body.get("result") if isinstance(body, dict) else None
  return result if isinstance(result, dict) else {}


class QueuedNotifier:
  """Wraps a :class:`Notifier` so sending never blocks the caller.

  ``api.telegram.org`` is throttled or filtered on plenty of networks: the TCP
  connection is accepted and then no response arrives, so a send sits there for
  the whole ``TELEGRAM_HTTP_TIMEOUT`` before failing with
  ``httpx.ReadTimeout``. On the signal path that delay is not cosmetic — the
  JetStream ``SignalWorker`` processes envelopes one at a time, so every
  further signal's fan-out to the trading workers waits behind a notification
  nobody is reading yet.

  ``send_message`` therefore only queues the text and returns; a single
  background task performs the real sends, in order, at whatever pace Telegram
  allows. The queue is bounded — under a long outage the oldest text is worth
  more than an unbounded backlog, so a full queue drops the message with a
  warning rather than blocking the pipeline it was meant to stay out of.
  """

  def __init__(self, inner: Notifier, *, maxsize: int = 200) -> None:
    self._inner = inner
    self._queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue(maxsize=maxsize)
    self._task: asyncio.Task[None] | None = None

  @property
  def pending(self) -> int:
    return self._queue.qsize()

  async def start(self) -> None:
    """Launch the drain task; safe to call once per app lifetime."""
    if self._task is not None and not self._task.done():
      return
    self._task = asyncio.create_task(self._worker(), name="queued-notifier")

  async def stop(self, drain_timeout: float = 3.0) -> None:
    """Give the backlog a short grace period to flush, then cancel."""
    if self._task is None:
      return
    try:
      await asyncio.wait_for(self._queue.join(), timeout=drain_timeout)
    except asyncio.TimeoutError:
      logger.warning(
        "Notification queue still holds %d message(s) at shutdown", self.pending
      )
    self._task.cancel()
    try:
      await self._task
    except asyncio.CancelledError:
      pass
    self._task = None

  async def send_message(self, message_text: str, chat_id: str | None = None) -> bool:
    """Queue *message_text*. Returns False only when the backlog is full."""
    try:
      self._queue.put_nowait((message_text, chat_id))
    except asyncio.QueueFull:
      logger.warning(
        "Notification queue full (%d) — dropping message", self._queue.maxsize
      )
      return False
    return True

  async def _worker(self) -> None:
    while True:
      message_text, chat_id = await self._queue.get()
      try:
        await self._inner.send_message(message_text, chat_id)
      except asyncio.CancelledError:
        raise
      except Exception:
        # send_message already logs its own failures; never let one kill the
        # drain task, or notifications stop silently for the whole process.
        pass
      finally:
        self._queue.task_done()


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
