"""
broker/db/repository/trade.py — trades reported by workers on ``TRADE``
(``SqlAlchemyTradeRepository``).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from broker.db.models import (
  Account,
  AccountBotLink,
  Trade,
)
from broker.domain.trade_status import TradeStatusPolicy
from broker.db.repository import _session
from broker.logger import get_logger
from broker.schemas.account_schema import MarketTypeEnum
from broker.schemas.core import BotPlatformTypeEnum
from broker.schemas.trade_event_schema import PositionEvent

log = get_logger(__name__)


class SqlAlchemyTradeRepository:
  """Applies worker position events to the ``trades`` table (and refreshes the
  owning ``accounts`` row)."""

  def __init__(self, policy: TradeStatusPolicy | None = None) -> None:
    self._policy = policy or TradeStatusPolicy()

  async def _upsert_account(self, session: AsyncSession, event: PositionEvent) -> None:
    """Upsert the accounts row within an existing session. Always refreshes
    last_activity_at.

    Scoped by account_id + market (see ``upsert_gateway`` for why
    account_id alone isn't enough), matching either the exact gateway already
    on file or a legacy row whose gateway is still NULL.
    """
    market = MarketTypeEnum(event.market)
    result = await session.execute(
      select(Account).where(
        Account.account_id == event.account_id,
        Account.market == market,
        or_(Account.gateway == event.gateway, Account.gateway.is_(None)),
      )
    )
    row: Optional[Account] = result.scalars().first()
    now = datetime.now(timezone.utc)

    if row is not None:
      if event.account_name is not None:
        row.account_name = event.account_name
      row.market = market
      if event.gateway is not None:
        row.gateway = event.gateway
      if event.account_balance is not None:
        row.account_balance = event.account_balance
      row.last_activity_at = now
    else:
      session.add(
        Account(
          id=uuid.uuid4(),
          account_id=event.account_id,
          account_name=event.account_name,
          account_balance=event.account_balance,
          market=market,
          gateway=event.gateway,
          last_activity_at=now,
        )
      )

  async def upsert_by_position_event(self, event: PositionEvent) -> Trade | None:
    """Apply a PositionEvent received from the worker (via NATS TRADE) to the
    broker's `trades` table. Performs an upsert keyed by (market, gateway,
    account_id, ref_id) — account_id alone doesn't identify an account
    uniquely (see ``uq_accounts_market_gateway_account_id``); updates the row
    if it exists, otherwise inserts a new one. Idempotent."""
    trade_status = self._policy.to_trade_status(event.status)
    if trade_status is None:
      log.warning("upsert_by_position_event: unknown position status=%s", event.status)
      return None

    # TP2 / SL / R_SL all persist as CLOSED and ``action`` keeps the entry
    # direction, so the event's own label is the only record of *how* the trade
    # moved. Stored on the row because the live trade card is re-rendered later
    # with nothing but the row to go on.
    last_action = self._policy.to_last_action(event.status)
    is_running = self._policy.is_open(event.status)
    # A close price of 0 means the worker had none to report (seen on FLATTED
    # events) — no instrument ever closes at 0, so treat it like a missing
    # value and keep the open price rather than persisting a bogus 0.
    price = event.closed_price if event.closed_price else event.opened_price
    market = MarketTypeEnum(event.market)

    async with _session.get_session() as session:
      await self._upsert_account(session, event)

      result = await session.execute(
        select(Trade).where(
          Trade.account_id == event.account_id,
          Trade.ref_id == event.ref_source_id,
          Trade.market == market,
          or_(Trade.gateway == event.gateway, Trade.gateway.is_(None)),
        )
      )
      row: Optional[Trade] = result.scalars().first()

      if row is not None:
        if self._policy.is_downgrade(trade_status, row.status):
          log.warning(
            "upsert_by_position_event: ignoring status downgrade %s → %s "
            "for account_id=%s ref_id=%s",
            row.status,
            trade_status,
            event.account_id,
            event.ref_source_id,
          )
          return row
        row.status = trade_status
        row.last_action = last_action
        row.is_running = is_running
        row.price = price
        row.quantity = event.volume
        row.market = market
        if event.gateway is not None:
          row.gateway = event.gateway
        if event.reject_reason is not None:
          row.reject_reason = event.reject_reason
        if event.comment is not None:
          row.comment = event.comment
        if event.account_balance is not None:
          row.account_balance = event.account_balance
        if event.gateway_return_code is not None:
          row.gateway_return_code = event.gateway_return_code
        await session.flush()
        await session.refresh(row)
        log.debug(
          "trade upserted (update) id=%s account_id=%s ref_id=%s status=%s",
          str(row.id),
          event.account_id,
          event.ref_source_id,
          trade_status,
        )
        return row

      new_row = Trade(
        id=uuid.uuid4(),
        account_id=event.account_id,
        market=market,
        gateway=event.gateway,
        account_leverage=event.account_leverage,
        account_balance_init=event.account_balance,
        account_balance=event.account_balance,
        ref_id=event.ref_source_id,
        comment=event.comment,
        gateway_return_code=event.gateway_return_code,
        strategy=event.strategy,
        strategy_code=event.strategy_code or "",
        symbol=event.symbol,
        action=event.action.upper(),
        price=price,
        quantity=event.volume,
        sl=event.sl,
        tp1=event.tp1,
        tp2=event.tp2,
        is_running=is_running,
        risk_percent=event.risk_percent,
        status=trade_status,
        last_action=last_action,
        reject_reason=event.reject_reason,
      )
      session.add(new_row)
      await session.flush()
      await session.refresh(new_row)
      log.debug(
        "trade upserted (insert) id=%s account_id=%s ref_id=%s status=%s",
        str(new_row.id),
        event.account_id,
        event.ref_source_id,
        trade_status,
      )
      return new_row

  async def list_by_account(
    self,
    account_id: str,
    limit: int,
    offset: int,
    order: str = "desc",
    order_by: str = "updatedAt",
  ) -> list[Trade]:
    """Return trades for an account with offset/limit pagination and configurable sort.

    KNOWN LIMITATION: filters by bare ``account_id`` only, which can now match
    trades from more than one account if the id was reused across gateways
    (see ``rotate_link_token``'s docstring). Not scoped to market/gateway
    here — the admin-facing callers (bot ``/atrades``, ``GET
    /v1/{account_id}/trades``) would need to pass those too, a wider change
    than this migration covers.
    """
    _sortable = {
      "updatedAt": Trade.updatedAt,
      "createdAt": Trade.createdAt,
      "status": Trade.status,
      "symbol": Trade.symbol,
    }
    col = _sortable.get(order_by, Trade.updatedAt)
    sort_expr = col.desc() if order == "desc" else col.asc()
    try:
      async with _session.get_session() as session:
        result = await session.execute(
          select(Trade)
          .where(Trade.account_id == account_id)
          .order_by(sort_expr)
          .offset(offset)
          .limit(limit)
        )
        return list(result.scalars().all())
    except Exception as exc:
      log.exception("Failed to fetch trades for account_id=%s: %s", account_id, exc)
      return []

  async def get_for_telegram_user(
    self,
    trade_id: uuid.UUID,
    telegram_user_id: int,
    platform: BotPlatformTypeEnum = BotPlatformTypeEnum.TELEGRAM,
  ) -> Trade | None:
    """Return the trade *trade_id* only if the bot user may see it.

    "May see it" means the trade's owning account — matched on
    ``account_id`` + ``market`` + ``gateway`` the way
    ``list_broadcast_targets`` does — is one this user is linked to. Any of
    their linked accounts qualifies, not just the active one: a trade card
    stays actionable after the owner has switched to another account.

    Returning None for both "no such trade" and "not yours" is deliberate —
    the endpoint answers 404 either way rather than confirming that some other
    owner's trade id exists.
    """
    try:
      async with _session.get_session() as session:
        result = await session.execute(
          select(Trade)
          .join(
            Account,
            (Account.account_id == Trade.account_id)
            & (Account.market == Trade.market)
            & (or_(Account.gateway == Trade.gateway, Account.gateway.is_(None))),
          )
          .join(AccountBotLink, AccountBotLink.account_id == Account.id)
          .where(
            Trade.id == trade_id,
            AccountBotLink.platform == platform,
            AccountBotLink.platform_user_id == str(telegram_user_id),
          )
        )
        return result.scalars().first()
    except Exception as exc:
      log.exception(
        "Failed to fetch trade id=%s for telegram_user_id=%s: %s",
        trade_id,
        telegram_user_id,
        exc,
      )
      return None

  async def count_by_account(self, account_id: str) -> int:
    """Return the total number of trades for an account.

    Same bare-``account_id`` limitation as ``list_by_account``.
    """
    try:
      async with _session.get_session() as session:
        result = await session.execute(
          select(func.count()).select_from(Trade).where(Trade.account_id == account_id)
        )
        return result.scalar_one()
    except Exception as exc:
      log.exception("Failed to count trades for account_id=%s: %s", account_id, exc)
      return 0

  async def list_open_by_account(self, account_id: str) -> list[Trade]:
    """Return currently running (``is_running``) trades for an account, most
    recently updated first. Unpaginated — the number of positions a single
    account can have open at once is small by construction (one per
    symbol/strategy the worker allows).

    Same bare-``account_id`` limitation as ``list_by_account``.
    """
    try:
      async with _session.get_session() as session:
        result = await session.execute(
          select(Trade)
          .where(Trade.account_id == account_id)
          .where(Trade.is_running.is_(True))
          .order_by(Trade.updatedAt.desc())
        )
        return list(result.scalars().all())
    except Exception as exc:
      log.exception(
        "Failed to fetch open trades for account_id=%s: %s", account_id, exc
      )
      return []

  async def list_distinct_strategies(self) -> list[str]:
    """Distinct non-empty ``strategy`` values seen on the ``trades`` table,
    alphabetically. Backs the admin FLAT strategy picker — a FLAT scoped to a
    strategy the broker has never observed is worthless, so the picker only
    offers ones a trade has actually carried."""
    try:
      async with _session.get_session() as session:
        result = await session.execute(
          select(Trade.strategy)
          .where(Trade.strategy.is_not(None))
          .where(Trade.strategy != "")
          .distinct()
          .order_by(Trade.strategy.asc())
        )
        return list(result.scalars().all())
    except Exception as exc:
      log.exception("Failed to list distinct strategies: %s", exc)
      return []
