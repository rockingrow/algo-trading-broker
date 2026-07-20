"""
broker/services/trade_broadcast_service.py — Owner completed-trade broadcasts.

When a worker's TRADE event closes a trade, the account's owner(s) who have
opted in (``/subscribe`` in the bot) get a Telegram DM with the completed
trade. Kept out of ``TradeEventConsumer`` so the consumer stays about applying
events to the DB, and so the broadcast decision (which statuses count as
"completed", who to notify, how to format) can be unit-tested on its own.

Delivery is best-effort: a lookup failure or a failed send is logged and never
propagates back into the TRADE consumer, which must still persist the event.
"""

from __future__ import annotations

from broker.constants import NOTIFICATION_TIMEZONE_KEY
from broker.db.models import Trade
from broker.domain.trade_status import TradeStatusPolicy
from broker.helpers.message_formatter import format_completed_trade_message
from broker.interfaces import SettingRepository, TradeBroadcastRepository
from broker.logger import get_logger
from broker.schemas.trade_event_schema import PositionEvent
from broker.schemas.trade_schema import TradeStatusEnum
from broker.services.notification_service import OwnerBroadcastNotifier

log = get_logger(__name__)


class TradeBroadcastService:
  """Decides whether a TRADE event completes a trade and DMs subscribed owners."""

  def __init__(
    self,
    *,
    broadcast_repository: TradeBroadcastRepository,
    setting_repository: SettingRepository,
    notifier: OwnerBroadcastNotifier | None = None,
    policy: TradeStatusPolicy | None = None,
  ) -> None:
    self._broadcasts = broadcast_repository
    self._settings = setting_repository
    self._notifier = notifier or OwnerBroadcastNotifier()
    self._policy = policy or TradeStatusPolicy()

  def _is_completion(self, event: PositionEvent) -> bool:
    """A trade is "completed" when this event maps to a fully-CLOSED status.

    Gating on the event's own status (not the persisted row's) keys the
    broadcast to the discrete close event the worker emits once — TP2 / SL /
    R_SL / TERMINAL_CLOSED / FORCED_CLOSED — instead of firing again on any
    later touch of an already-closed row.
    """
    return self._policy.to_trade_status(event.status) == TradeStatusEnum.CLOSED

  async def maybe_broadcast(self, event: PositionEvent, trade: Trade | None) -> None:
    """DM every subscribed owner of ``trade``'s account when the event closes it."""
    if trade is None or not self._is_completion(event):
      return

    try:
      targets = await self._broadcasts.list_broadcast_targets(
        account_id=trade.account_id,
        market=trade.market,
        gateway=trade.gateway,
      )
    except Exception as exc:
      log.exception(
        "Broadcast target lookup failed account_id=%s: %s", trade.account_id, exc
      )
      return

    if not targets:
      return

    timezone_offset = await self._settings.get(NOTIFICATION_TIMEZONE_KEY)
    message = format_completed_trade_message(trade, timezone_offset=timezone_offset)

    for chat_id in targets:
      await self._notifier.send_to(chat_id, message)

    log.info(
      "Broadcast completed trade account_id=%s ref_id=%s to %d owner(s)",
      trade.account_id,
      trade.ref_id,
      len(targets),
    )
