"""
broker/db/repository.py
────────────────────────
Async persistence layer. Each repository class is a thin data-access wrapper
around SQLAlchemy sessions and implements a Protocol declared in
``broker/interfaces.py`` so that callers depend on the abstraction, not on
these concrete classes.

Business rules about the trade lifecycle live in
``broker/domain/trade_status.py`` — this module only reads and writes rows.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from broker.db.engine import get_session
from broker.db.models import Account, BrokerSetting, Signal, Trade
from broker.domain.trade_status import TradeStatusPolicy
from broker.logger import get_logger
from broker.schemas.account_schema import MarketTypeEnum
from broker.schemas.trade_event_schema import PositionEvent
from broker.schemas.webhook_schema import WebhookPayload

log = get_logger(__name__)


class SqlAlchemyAccountRepository:
  """Reads rows from the ``accounts`` table."""

  async def get_all(self) -> list[Account]:
    """Return all accounts ordered by last_activity_at desc."""
    try:
      async with get_session() as session:
        result = await session.execute(
          select(Account).order_by(Account.last_activity_at.desc())
        )
        return list(result.scalars().all())
    except Exception as exc:
      log.exception("Failed to fetch accounts: %s", exc)
      return []


class SqlAlchemySettingRepository:
  """Reads and upserts rows in the ``broker_settings`` table."""

  async def get(self, key: str) -> str | None:
    """Return the value for the given key, or None if not found."""
    try:
      async with get_session() as session:
        result = await session.execute(
          select(BrokerSetting).where(BrokerSetting.key == key)
        )
        row: Optional[BrokerSetting] = result.scalars().first()
        return row.value if row is not None else None
    except Exception as exc:
      log.exception("Failed to read broker setting key=%s: %s", key, exc)
      return None

  async def set(self, key: str, value: str) -> bool:
    """Upsert a broker_settings row. Returns True on success, False on error."""
    try:
      async with get_session() as session:
        result = await session.execute(
          select(BrokerSetting).where(BrokerSetting.key == key)
        )
        row: Optional[BrokerSetting] = result.scalars().first()
        if row is not None:
          row.value = value
        else:
          session.add(BrokerSetting(id=uuid.uuid4(), key=key, value=value))
      log.debug("broker setting upserted key=%s value=%s", key, value)
      return True
    except Exception as exc:
      log.exception("Failed to upsert broker setting key=%s: %s", key, exc)
      return False


class SqlAlchemySignalRepository:
  """Persists inbound TradingView webhook signals to the ``signals`` table."""

  async def log_signal(self, payload: WebhookPayload) -> str | None:
    """
    Persist a received TradingView webhook signal.

    Returns
    -------
    str | None
        The UUID of the inserted row, or None if the insert failed.
    """
    pos = payload.position
    new_id = uuid.uuid4()
    row = Signal(
      id=new_id,
      strategy=payload.strategy,
      symbol=payload.symbol,
      timeframe=payload.timeframe,
      timestamp=payload.timestamp,
      action=pos.action,
      price=pos.price or 0.0,
      quantity=pos.quantity or 0.0,
      sl=pos.sl,
      tp1=pos.tp1,
      tp2=pos.tp2,
      is_running=pos.is_running if pos.is_running is not None else False,
      risk_percent=payload.inputs.risk_percent
      if payload.inputs is not None and payload.inputs.risk_percent is not None
      else 0.0,
      indicators=json.loads(payload.indicators.model_dump_json())
      if payload.indicators is not None
      else {},
      inputs=json.loads(payload.inputs.model_dump_json())
      if payload.inputs is not None
      else {},
      raw=json.loads(payload.model_dump_json()),
    )
    try:
      async with get_session() as session:
        session.add(row)
      log.debug("signals written for symbol=%s id=%s", payload.symbol, str(new_id))
      return str(new_id)
    except Exception as exc:
      log.exception("Failed to write signals: %s", exc)
      return None


class SqlAlchemyTradeRepository:
  """Applies worker position events to the ``trades`` table (and refreshes the
  owning ``accounts`` row)."""

  def __init__(self, policy: TradeStatusPolicy | None = None) -> None:
    self._policy = policy or TradeStatusPolicy()

  async def _upsert_account(self, session: AsyncSession, event: PositionEvent) -> None:
    """Upsert the accounts row within an existing session. Always refreshes
    last_activity_at."""
    result = await session.execute(
      select(Account).where(Account.account_id == event.account_id)
    )
    row: Optional[Account] = result.scalars().first()
    now = datetime.now(timezone.utc)

    if row is not None:
      if event.account_name is not None:
        row.account_name = event.account_name
      row.market_type = MarketTypeEnum(event.market_type)
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
          market_type=MarketTypeEnum(event.market_type),
          last_activity_at=now,
        )
      )

  async def upsert_by_position_event(self, event: PositionEvent) -> Trade | None:
    """Apply a PositionEvent received from the worker (via NATS TRADE) to the
    broker's `trades` table. Performs an upsert keyed by (account_id, ref_id):
    updates the row if it exists, otherwise inserts a new one. Idempotent."""
    trade_status = self._policy.to_trade_status(event.status)
    if trade_status is None:
      log.warning("upsert_by_position_event: unknown position status=%s", event.status)
      return None

    is_running = self._policy.is_open(event.status)
    price = event.closed_price if event.closed_price is not None else event.opened_price

    async with get_session() as session:
      await self._upsert_account(session, event)

      result = await session.execute(
        select(Trade).where(
          Trade.account_id == event.account_id,
          Trade.ref_id == event.ref_source_id,
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
        row.is_running = is_running
        row.price = price
        row.quantity = event.volume
        if event.comment is not None:
          row.comment = event.comment
        if event.account_balance is not None:
          row.account_balance = event.account_balance
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

      if event.account_leverage is None:
        log.error(
          "upsert_by_position_event: cannot create Trade for "
          "account_id=%s ref_id=%s without account_leverage",
          event.account_id,
          event.ref_source_id,
        )
        return None

      new_row = Trade(
        id=uuid.uuid4(),
        account_id=event.account_id,
        account_leverage=event.account_leverage,
        account_balance_init=event.account_balance,
        account_balance=event.account_balance,
        ref_id=event.ref_source_id,
        comment=event.comment,
        strategy_code=event.strategy_code or f"{event.action}|{event.signal_id or event.ref_source_id}",
        strategy=event.strategy,
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
        reject_reason=None,
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
    """Return trades for an account with offset/limit pagination and configurable sort."""
    _sortable = {
      "updatedAt": Trade.updatedAt,
      "createdAt": Trade.createdAt,
      "status": Trade.status,
      "symbol": Trade.symbol,
    }
    col = _sortable.get(order_by, Trade.updatedAt)
    sort_expr = col.desc() if order == "desc" else col.asc()
    try:
      async with get_session() as session:
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

  async def count_by_account(self, account_id: str) -> int:
    """Return the total number of trades for an account."""
    try:
      async with get_session() as session:
        result = await session.execute(
          select(func.count()).select_from(Trade).where(Trade.account_id == account_id)
        )
        return result.scalar_one()
    except Exception as exc:
      log.exception("Failed to count trades for account_id=%s: %s", account_id, exc)
      return 0
