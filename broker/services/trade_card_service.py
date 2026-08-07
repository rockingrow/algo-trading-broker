"""
broker/services/trade_card_service.py — Live trade cards for account owners.

Every TRADE event a worker publishes lands here after the trade row has been
upserted. For each owner of that account who has opted in (``/subscribe`` in
the bot) the service keeps exactly **one** Telegram message alive per trade:

* first sighting  → post the card, with a Detail / Exit button row,
* status change   → edit that same message in place (OPENED → PARTIALLY_CLOSED
  → CLOSED / FLAT / REJECTED),
* terminal status → edit one last time and drop the buttons, since there is
  nothing left to act on.

This replaces the old "DM once, when the trade completes" broadcast: a closed
trade is now simply the final state of a card the owner has been watching all
along, so the completion is still delivered, in the message they already have.

**Nothing Telegram-shaped happens on the TRADE path.** nats-py awaits this
consumer's callback before pulling the next TRADE message, and a DM to a
throttled ``api.telegram.org`` sits there for the whole HTTP timeout — the same
reason the signal path hides behind ``QueuedNotifier``. That decorator is no
help here (a card needs the ``message_id`` its send returns, and an edit needs
the state that follows), so this service owns the same shape one level up:
:meth:`handle_event` only queues the trade, and a single background task does
the lookups and the Bot API calls. One drain task, FIFO, so two events for the
same trade can never be applied out of order.

Two rules keep the traffic proportional to what a human would notice:

* **Only status transitions edit.** Workers re-emit TRADE events for changes a
  card doesn't show (an SL nudge, a sync tick). ``trade_notifications.status``
  records what the message currently displays, so those are skipped.
* **Existing cards are refreshed even after unsubscribing.** The opt-in decides
  who gets a *new* card; once a card exists it must keep telling the truth, or
  a user who unsubscribes mid-trade is left with a message that says OPENED
  and offers an Exit button forever.

Delivery is best-effort throughout: a lookup or send failure is logged and
never propagates back into the TRADE consumer, which must still persist the
event.
"""

from __future__ import annotations

import asyncio

from broker.constants import NOTIFICATION_TIMEZONE_KEY
from broker.db.models import Trade
from broker.helpers.trade_card import (
  format_trade_card,
  trade_card_keyboard,
  trade_status,
)
from broker.interfaces import (
  SettingRepository,
  TradeBroadcastRepository,
  TradeNotificationRepository,
)
from broker.logger import get_logger
from broker.schemas.trade_event_schema import PositionEvent
from broker.schemas.trade_schema import TradeCard, TradeStatusEnum
from broker.services.notification_service import (
  ChatTarget,
  EditOutcome,
  TradeCardNotifier,
)

log = get_logger(__name__)

#: Bound on the hand-off queue. Under a Telegram outage the trades themselves
#: are already safe in Postgres — a card is a notification, so dropping the
#: newest rather than growing without limit is the right trade-off, and matches
#: what ``QueuedNotifier`` does on the signal path.
_QUEUE_MAXSIZE = 200


class TradeCardService:
  """Posts and maintains one live Telegram card per trade, per subscribed owner."""

  def __init__(
    self,
    *,
    broadcast_repository: TradeBroadcastRepository,
    notification_repository: TradeNotificationRepository,
    setting_repository: SettingRepository,
    notifier: TradeCardNotifier | None = None,
    maxsize: int = _QUEUE_MAXSIZE,
  ) -> None:
    self._broadcasts = broadcast_repository
    self._cards = notification_repository
    self._settings = setting_repository
    self._notifier = notifier or TradeCardNotifier()
    self._queue: asyncio.Queue[Trade] = asyncio.Queue(maxsize=maxsize)
    self._task: asyncio.Task[None] | None = None

  # ── lifecycle (called from the app lifespan) ─────────────────────

  @property
  def pending(self) -> int:
    return self._queue.qsize()

  async def start(self) -> None:
    """Launch the drain task; safe to call once per app lifetime."""
    if self._task is not None and not self._task.done():
      return
    self._task = asyncio.create_task(self._worker(), name="trade-cards")

  async def stop(self, drain_timeout: float = 3.0) -> None:
    """Give the backlog a short grace period to flush, then cancel."""
    if self._task is None:
      return
    try:
      await asyncio.wait_for(self._queue.join(), timeout=drain_timeout)
    except asyncio.TimeoutError:
      log.warning("Trade card queue still holds %d update(s) at shutdown", self.pending)
    self._task.cancel()
    try:
      await self._task
    except asyncio.CancelledError:
      pass
    self._task = None

  # ── TRADE path (must stay non-blocking) ──────────────────────────

  async def handle_event(self, event: PositionEvent, trade: Trade | None) -> None:
    """Queue ``trade``'s card for refresh. Returns immediately.

    The *persisted* trade is what gets rendered, not the event: the repository
    already refused any status downgrade, so rendering the row is what keeps a
    late or out-of-order event from reopening a card that has been closed. The
    row is detached but the session is configured ``expire_on_commit=False``,
    so the worker can read it after the transaction has closed.
    """
    if trade is None:
      return
    try:
      self._queue.put_nowait(trade)
    except asyncio.QueueFull:
      log.warning(
        "Trade card queue full (%d) — dropping update for account_id=%s ref_id=%s",
        self._queue.maxsize,
        trade.account_id,
        trade.ref_id,
      )

  async def _worker(self) -> None:
    while True:
      trade = await self._queue.get()
      try:
        await self._apply(trade)
      except asyncio.CancelledError:
        raise
      except Exception as exc:
        # Never let one bad trade kill the drain task, or cards stop silently
        # for the whole process.
        log.exception(
          "Failed to update trade card account_id=%s ref_id=%s: %s",
          trade.account_id,
          trade.ref_id,
          exc,
        )
      finally:
        self._queue.task_done()

  # ── delivery (background only) ───────────────────────────────────

  async def _apply(self, trade: Trade) -> None:
    """Bring every card for ``trade`` up to date."""
    existing = await self._cards.list_for_trade(trade.id)
    subscribers = await self._broadcasts.list_broadcast_targets(
      account_id=trade.account_id,
      market=trade.market,
      gateway=trade.gateway,
    )
    if not existing and not subscribers:
      return

    timezone_offset = await self._settings.get(NOTIFICATION_TIMEZONE_KEY)
    body = format_trade_card(trade, timezone_offset=timezone_offset)
    markup = trade_card_keyboard(trade)
    status = trade_status(trade)

    carded = {card.chat_id for card in existing}
    edited = 0
    for card in existing:
      if await self._refresh(card, body, markup, status):
        edited += 1

    posted = 0
    for chat_id in subscribers:
      if chat_id in carded:
        continue
      if await self._post(chat_id, trade, body, markup, status):
        posted += 1

    if posted or edited:
      log.info(
        "Trade card account_id=%s ref_id=%s status=%s posted=%d edited=%d",
        trade.account_id,
        trade.ref_id,
        status.value,
        posted,
        edited,
      )

  async def _refresh(
    self,
    card: TradeCard,
    body: str,
    markup: dict | None,
    status: TradeStatusEnum,
  ) -> bool:
    """Edit one existing card, unless it already shows this status."""
    if card.status == status:
      return False

    outcome = await self._notifier.edit_message(
      ChatTarget(card.chat_id),
      str(card.message_id),
      body,
      reply_markup=markup,
    )
    if outcome is EditOutcome.OK:
      await self._cards.mark_status(card.id, status)
      return True
    if outcome is EditOutcome.MISSING:
      # A DM that can no longer be edited cannot be re-sent either — the user
      # deleted it, or blocked the bot (see ``_DM_GONE_MARKERS``). Forget it so
      # later status changes stop retrying.
      await self._cards.delete(card.id)
    return False

  async def _post(
    self,
    chat_id: str,
    trade: Trade,
    body: str,
    markup: dict | None,
    status: TradeStatusEnum,
  ) -> bool:
    """Post a first card for one subscriber.

    A subscriber can meet a trade mid-life — they opted in after it opened, or
    the card was deleted — so this is not restricted to freshly opened trades.
    A trade that is already over still gets its card; it simply arrives without
    buttons, which is exactly the completion DM the old broadcast sent.
    """
    message_id = await self._notifier.send_and_get_message_id(
      ChatTarget(chat_id), body, reply_markup=markup
    )
    if message_id is None:
      return False
    await self._cards.record(
      trade_id=trade.id,
      chat_id=chat_id,
      message_id=int(message_id),
      status=status,
    )
    return True
