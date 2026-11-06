"""
broker/db/repository/trade_notification.py — the live trade cards per chat
(``SqlAlchemyTradeNotificationRepository``).
"""

from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import delete, select

from broker.db.models import TradeNotification
from broker.db.repository import _session
from broker.logger import get_logger
from broker.schemas.core import BotPlatformTypeEnum
from broker.schemas.trade_schema import TradeStatusEnum
from broker.schemas.trade_schema import TradeCard

log = get_logger(__name__)


class SqlAlchemyTradeNotificationRepository:
  """Tracks the Telegram message that carries each trade's live card.

  Every method swallows its exceptions and degrades to "no card known": a
  notification is cosmetic, and a bookkeeping failure must never stop the TRADE
  consumer from persisting the event that triggered it.
  """

  async def list_for_trade(
    self,
    trade_id: uuid.UUID,
    platform: BotPlatformTypeEnum = BotPlatformTypeEnum.TELEGRAM,
  ) -> list[TradeCard]:
    """Cards already posted for *trade_id*, one per recipient."""
    try:
      async with _session.get_session() as session:
        result = await session.execute(
          select(TradeNotification).where(
            TradeNotification.trade_id == trade_id,
            TradeNotification.platform == platform,
          )
        )
        return [TradeCard.model_validate(row) for row in result.scalars().all()]
    except Exception as exc:
      log.exception("Failed to list trade cards for trade_id=%s: %s", trade_id, exc)
      return []

  async def record(
    self,
    trade_id: uuid.UUID,
    chat_id: str,
    message_id: int,
    status: TradeStatusEnum,
    platform: BotPlatformTypeEnum = BotPlatformTypeEnum.TELEGRAM,
  ) -> bool:
    """Remember a freshly posted card. Idempotent — a second call for the same
    recipient updates the existing row rather than violating the unique key,
    which is what happens if a card was posted, deleted by the user, and then
    re-posted."""
    try:
      async with _session.get_session() as session:
        result = await session.execute(
          select(TradeNotification).where(
            TradeNotification.trade_id == trade_id,
            TradeNotification.platform == platform,
            TradeNotification.chat_id == chat_id,
          )
        )
        row: Optional[TradeNotification] = result.scalars().first()
        if row is not None:
          row.message_id = message_id
          row.status = status
        else:
          session.add(
            TradeNotification(
              id=uuid.uuid4(),
              trade_id=trade_id,
              platform=platform,
              chat_id=chat_id,
              message_id=message_id,
              status=status,
            )
          )
      return True
    except Exception as exc:
      log.exception(
        "Failed to record trade card trade_id=%s chat_id=%s: %s",
        trade_id,
        chat_id,
        exc,
      )
      return False

  async def mark_status(self, card_id: uuid.UUID, status: TradeStatusEnum) -> bool:
    """Record the status the card now displays, after a successful edit."""
    try:
      async with _session.get_session() as session:
        row = await session.get(TradeNotification, card_id)
        if row is None:
          return False
        row.status = status
      return True
    except Exception as exc:
      log.exception("Failed to update trade card id=%s: %s", card_id, exc)
      return False

  async def delete(self, card_id: uuid.UUID) -> bool:
    """Forget a card whose message Telegram says is gone for good."""
    try:
      async with _session.get_session() as session:
        await session.execute(
          delete(TradeNotification).where(TradeNotification.id == card_id)
        )
      return True
    except Exception as exc:
      log.exception("Failed to delete trade card id=%s: %s", card_id, exc)
      return False
