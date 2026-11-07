"""
broker/db/repository/trade_broadcast.py — owner subscriptions to trade cards
(``SqlAlchemyTradeBroadcastRepository``).
"""

from __future__ import annotations

import uuid

from sqlalchemy import or_, select, update

from broker.db.models import (
  Account,
  AccountBotLink,
)
from broker.db.repository import _session
from broker.logger import get_logger
from broker.schemas.account_schema import MarketTypeEnum
from broker.schemas.core import BotPlatformTypeEnum

log = get_logger(__name__)


class SqlAlchemyTradeBroadcastRepository:
  """Manages the per-account opt-in for live trade-card DMs, and resolves which
  users should be sent one for a given account.

  The opt-in is ``AccountBotLink.trade_cards_enabled``: one flag per
  ``(user, account)`` link. The API layer points it at the user's *active*
  account, so ``/subscribe`` on account A says nothing about account B;
  :meth:`subscribe_all` / :meth:`unsubscribe_all` are the bulk form, covering
  every link the user holds right now. It lives on the link, which means
  unlinking (or an admin token rotation) deletes the preference together with
  the access it belonged to."""

  async def _set_enabled(
    self,
    telegram_user_id: int,
    account_uuid: uuid.UUID,
    platform: BotPlatformTypeEnum,
    enabled: bool,
  ) -> bool:
    platform_user_id = str(telegram_user_id)
    try:
      async with _session.get_session() as session:
        result = await session.execute(
          update(AccountBotLink)
          .where(
            AccountBotLink.platform == platform,
            AccountBotLink.platform_user_id == platform_user_id,
            AccountBotLink.account_id == account_uuid,
          )
          .values(trade_cards_enabled=enabled)
        )
        if result.rowcount == 0:
          # The link vanished between resolving the active account and this
          # write (an unlink or a rotation) — nothing to toggle.
          log.warning(
            "Trade cards not %s: telegram_user_id=%s holds no link to account=%s",
            "enabled" if enabled else "disabled",
            telegram_user_id,
            account_uuid,
          )
          return False
      log.info(
        "Trade cards %s telegram_user_id=%s account=%s",
        "enabled" if enabled else "disabled",
        telegram_user_id,
        account_uuid,
      )
      return True
    except Exception as exc:
      log.exception(
        "Failed to set trade cards enabled=%s telegram_user_id=%s account=%s: %s",
        enabled,
        telegram_user_id,
        account_uuid,
        exc,
      )
      return False

  async def _set_enabled_everywhere(
    self,
    telegram_user_id: int,
    platform: BotPlatformTypeEnum,
    enabled: bool,
  ) -> int | None:
    platform_user_id = str(telegram_user_id)
    try:
      async with _session.get_session() as session:
        result = await session.execute(
          update(AccountBotLink)
          .where(
            AccountBotLink.platform == platform,
            AccountBotLink.platform_user_id == platform_user_id,
          )
          .values(trade_cards_enabled=enabled)
        )
        links = result.rowcount
      log.info(
        "Trade cards %s on every link telegram_user_id=%s links=%s",
        "enabled" if enabled else "disabled",
        telegram_user_id,
        links,
      )
      return links
    except Exception as exc:
      log.exception(
        "Failed to set trade cards enabled=%s on every link telegram_user_id=%s: %s",
        enabled,
        telegram_user_id,
        exc,
      )
      return None

  async def subscribe_all(
    self,
    telegram_user_id: int,
    platform: BotPlatformTypeEnum = BotPlatformTypeEnum.TELEGRAM,
  ) -> int | None:
    """Opt a bot user in to live trade cards for **every** account they are
    linked to right now. Returns how many links it covers — ``0`` when the user
    holds none, ``None`` when the write failed. Idempotent. Links made later
    start off, as ever: this is a bulk switch, not a standing preference."""
    return await self._set_enabled_everywhere(telegram_user_id, platform, True)

  async def unsubscribe_all(
    self,
    telegram_user_id: int,
    platform: BotPlatformTypeEnum = BotPlatformTypeEnum.TELEGRAM,
  ) -> int | None:
    """Opt a bot user out of live trade cards for every account they are linked
    to. Same return contract as :meth:`subscribe_all`."""
    return await self._set_enabled_everywhere(telegram_user_id, platform, False)

  async def subscribe(
    self,
    telegram_user_id: int,
    account_uuid: uuid.UUID,
    platform: BotPlatformTypeEnum = BotPlatformTypeEnum.TELEGRAM,
  ) -> bool:
    """Opt a bot user in to live trade cards for one account. Idempotent —
    True whether the flag was flipped now or was already on; False when the
    user holds no link to ``account_uuid``."""
    return await self._set_enabled(telegram_user_id, account_uuid, platform, True)

  async def unsubscribe(
    self,
    telegram_user_id: int,
    account_uuid: uuid.UUID,
    platform: BotPlatformTypeEnum = BotPlatformTypeEnum.TELEGRAM,
  ) -> bool:
    """Opt a bot user out of live trade cards for one account. Idempotent —
    True whether the flag was flipped now or was already off; False when the
    user holds no link to ``account_uuid``."""
    return await self._set_enabled(telegram_user_id, account_uuid, platform, False)

  async def is_subscribed(
    self,
    telegram_user_id: int,
    account_uuid: uuid.UUID,
    platform: BotPlatformTypeEnum = BotPlatformTypeEnum.TELEGRAM,
  ) -> bool:
    """Whether a bot user is opted in to live trade cards for one account.
    A user who holds no link to it is simply not subscribed."""
    platform_user_id = str(telegram_user_id)
    try:
      async with _session.get_session() as session:
        result = await session.execute(
          select(AccountBotLink.trade_cards_enabled).where(
            AccountBotLink.platform == platform,
            AccountBotLink.platform_user_id == platform_user_id,
            AccountBotLink.account_id == account_uuid,
          )
        )
        return bool(result.scalars().first())
    except Exception as exc:
      log.exception(
        "Failed to read trade cards flag telegram_user_id=%s account=%s: %s",
        telegram_user_id,
        account_uuid,
        exc,
      )
      return False

  async def list_broadcast_targets(
    self,
    account_id: str,
    market: MarketTypeEnum | None,
    gateway: str | None,
    platform: BotPlatformTypeEnum = BotPlatformTypeEnum.TELEGRAM,
  ) -> list[str]:
    """Return the platform user ids that should receive a trade card for
    the account identified by ``(account_id, market, gateway)``: those whose
    link to a matching account has trade cards switched on.

    Scoped by ``account_id`` + ``market`` + ``gateway`` (with a NULL-gateway
    fallback, mirroring the trade/account upsert path) so a bare id reused
    across gateways doesn't leak a completion to the wrong owner.
    """
    try:
      async with _session.get_session() as session:
        account_filters = [Account.account_id == account_id]
        if market is not None:
          account_filters.append(Account.market == market)
        if gateway is not None:
          account_filters.append(
            or_(Account.gateway == gateway, Account.gateway.is_(None))
          )
        result = await session.execute(
          select(AccountBotLink.platform_user_id)
          .join(Account, Account.id == AccountBotLink.account_id)
          .where(
            AccountBotLink.platform == platform,
            AccountBotLink.trade_cards_enabled.is_(True),
            *account_filters,
          )
        )
        # dict.fromkeys de-dupes while keeping order — a user linked to two
        # matching account rows must be notified once, not twice.
        return list(dict.fromkeys(result.scalars().all()))
    except Exception as exc:
      log.exception(
        "Failed to resolve broadcast targets for account_id=%s: %s",
        account_id,
        exc,
      )
      return []
