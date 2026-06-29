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
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from broker.db.engine import get_session
from broker.db.models import Account, BrokerSetting, Signal, Trade
from broker.domain.trade_status import TradeStatusPolicy
from broker.logger import get_logger
from broker.schemas.account_schema import MarketTypeEnum
from broker.schemas.core import SignalStatusEnum
from broker.schemas.trade_event_schema import PositionEvent
from broker.schemas.webhook_schema import WebhookPayload
from broker.settings import settings

log = get_logger(__name__)


class SqlAlchemyAccountRepository:
  """Reads rows from the ``accounts`` table, and records the market/gateway a
  worker announces when it connects."""

  async def upsert_gateway(
    self, account_id: str, market: MarketTypeEnum, gateway: str
  ) -> None:
    """Persist the *market*/*gateway* a worker reported for *account_id*.

    Called on every ``WORKER_CONNECTED`` handshake. Without this, ``gateway`` is
    only ever written from a ``TRADE`` position event, so an account that has
    not traded since the column was introduced keeps a NULL gateway and cannot
    be addressed as ``<market>-<gateway>-<account_id>`` by the admin
    ``CRYPTO_LEVERAGE_INIT`` push.

    Inserts the row when the account is unknown (a worker that has connected but
    never traded is still addressable). Best-effort: failures are logged, never
    raised — the handshake reply matters more than this bookkeeping.
    """
    try:
      async with get_session() as session:
        result = await session.execute(
          select(Account).where(Account.account_id == account_id)
        )
        row: Optional[Account] = result.scalars().first()

        if row is not None:
          if row.market_type == market and row.gateway == gateway:
            return
          row.market_type = market
          row.gateway = gateway
        else:
          session.add(
            Account(
              id=uuid.uuid4(),
              account_id=account_id,
              market_type=market,
              gateway=gateway,
              last_activity_at=datetime.now(timezone.utc),
            )
          )

      log.debug(
        "account gateway upserted account_id=%s market=%s gateway=%s",
        account_id,
        market.value,
        gateway,
      )
    except Exception as exc:
      log.exception(
        "Failed to upsert gateway for account_id=%s: %s",
        account_id,
        exc,
      )

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

  async def get_by_market(self, market: MarketTypeEnum) -> list[Account]:
    """Return accounts in *market* ordered by last_activity_at desc."""
    try:
      async with get_session() as session:
        result = await session.execute(
          select(Account)
          .where(Account.market_type == market)
          .order_by(Account.last_activity_at.desc())
        )
        return list(result.scalars().all())
    except Exception as exc:
      log.exception("Failed to fetch accounts for market=%s: %s", market, exc)
      return []

  async def get_by_telegram_user_id(self, telegram_user_id: int) -> Account | None:
    """Return the account bound to a Telegram user, or None if unbound."""
    try:
      async with get_session() as session:
        result = await session.execute(
          select(Account).where(Account.telegram_user_id == telegram_user_id)
        )
        return result.scalars().first()
    except Exception as exc:
      log.exception(
        "Failed to fetch account for telegram_user_id=%s: %s",
        telegram_user_id,
        exc,
      )
      return None

  async def link_telegram(
    self, token: uuid.UUID, telegram_user_id: int
  ) -> Account | None:
    """Bind ``telegram_user_id`` to the account identified by ``token``.

    A Telegram user maps to at most one account, so any prior binding for the
    same ``telegram_user_id`` is released first (latest claim wins). Returns the
    linked account, or None if the token does not match any account.
    """
    try:
      async with get_session() as session:
        result = await session.execute(
          select(Account).where(Account.telegram_link_token == token)
        )
        account: Optional[Account] = result.scalars().first()
        if account is None:
          return None

        # Release the Telegram user from any other account before re-binding,
        # otherwise the unique constraint on telegram_user_id would conflict.
        others = await session.execute(
          select(Account).where(
            Account.telegram_user_id == telegram_user_id,
            Account.id != account.id,
          )
        )
        for other in others.scalars().all():
          other.telegram_user_id = None
        await session.flush()

        account.telegram_user_id = telegram_user_id
        await session.flush()
        await session.refresh(account)
        log.info(
          "Linked telegram_user_id=%s to account_id=%s",
          telegram_user_id,
          account.account_id,
        )
        return account
    except Exception as exc:
      log.exception(
        "Failed to link telegram_user_id=%s with token=%s: %s",
        telegram_user_id,
        token,
        exc,
      )
      return None

  async def unlink_telegram(self, telegram_user_id: int) -> bool:
    """Clear the Telegram binding for a user. Returns True if a row changed."""
    try:
      async with get_session() as session:
        result = await session.execute(
          select(Account).where(Account.telegram_user_id == telegram_user_id)
        )
        account: Optional[Account] = result.scalars().first()
        if account is None:
          return False
        account.telegram_user_id = None
        log.info("Unlinked telegram_user_id=%s", telegram_user_id)
        return True
    except Exception as exc:
      log.exception("Failed to unlink telegram_user_id=%s: %s", telegram_user_id, exc)
      return False

  async def rotate_link_token(self, account_id: str) -> uuid.UUID | None:
    """Issue a fresh link token for an account (revokes the old one).

    Returns the new token, or None if the account does not exist.
    """
    try:
      async with get_session() as session:
        result = await session.execute(
          select(Account).where(Account.account_id == account_id)
        )
        account: Optional[Account] = result.scalars().first()
        if account is None:
          return None
        new_token = uuid.uuid4()
        account.telegram_link_token = new_token
        log.info("Rotated link token for account_id=%s", account_id)
        return new_token
    except Exception as exc:
      log.exception(
        "Failed to rotate link token for account_id=%s: %s", account_id, exc
      )
      return None


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

  async def get_many(self, keys: list[str]) -> dict[str, str]:
    """Return {key: value} for every *keys* found in a single round trip;
    keys with no row are omitted (callers distinguish "missing" via absence).

    A single query also makes the read atomic under Postgres MVCC — every key
    reflects the same snapshot, unlike issuing one `get()` per key.
    """
    try:
      async with get_session() as session:
        result = await session.execute(
          select(BrokerSetting).where(BrokerSetting.key.in_(keys))
        )
        return {row.key: row.value for row in result.scalars().all()}
    except Exception as exc:
      log.exception("Failed to read broker settings keys=%s: %s", keys, exc)
      return {}

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
      # Mirror parse_signal's precedence: position-level risk_percent wins,
      # then inputs.risk_percent, else 0.0. Keeping these in sync ensures the
      # persisted audit row matches the signal that was actually published.
      risk_percent=pos.risk_percent
      if pos.risk_percent is not None
      else (
        payload.inputs.risk_percent
        if payload.inputs is not None and payload.inputs.risk_percent is not None
        else 0.0
      ),
      is_scale_position=bool(pos.is_scale_position),
      scale_strategy=pos.scale_strategy,
      status=SignalStatusEnum.QUEUED,
      attempts=settings.SIGNAL_MAX_ATTEMPTS,
      last_attempt=None,
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

  async def mark_published(self, signal_id: str) -> bool:
    """Flip a row from QUEUED to PUBLISHED once the JetStream handler is done.

    Returns ``True`` when the row exists and the update lands, ``False`` if the
    row is missing or the update fails. Missing is treated as ``False`` — the
    caller can log it, but this is best-effort audit metadata that never blocks
    the acknowledgment on JetStream.
    """
    try:
      row_id = uuid.UUID(signal_id)
    except (TypeError, ValueError):
      log.error("mark_published: invalid signal_id=%r", signal_id)
      return False

    try:
      async with get_session() as session:
        result = await session.execute(select(Signal).where(Signal.id == row_id))
        row: Optional[Signal] = result.scalars().first()
        if row is None:
          log.warning("mark_published: signal_id=%s not found", signal_id)
          return False
        row.status = SignalStatusEnum.PUBLISHED
      return True
    except Exception as exc:
      log.exception("Failed to mark signal published id=%s: %s", signal_id, exc)
      return False

  async def get_by_id(self, signal_id: str) -> Signal | None:
    """Return the persisted row for *signal_id*, or ``None`` when missing.

    Used by the retry job to rebuild a ``WebhookPayload`` from ``row.raw``
    before re-running the fan-out.
    """
    try:
      row_id = uuid.UUID(signal_id)
    except (TypeError, ValueError):
      log.error("get_by_id: invalid signal_id=%r", signal_id)
      return None

    try:
      async with get_session() as session:
        result = await session.execute(select(Signal).where(Signal.id == row_id))
        return result.scalars().first()
    except Exception as exc:
      log.exception("Failed to fetch signal id=%s: %s", signal_id, exc)
      return None

  async def record_attempt_failure(self, signal_id: str) -> Signal | None:
    """Consume one attempt on a failed fan-out.

    Decrements ``attempts`` and stamps ``last_attempt`` on the row. When the
    row was already at ``attempts == 1`` this call flips it to ``FAILED`` and
    zeroes the counter — the retry job's ``attempts > 0`` filter then stops
    re-picking it. Returns the updated row (or ``None`` if missing / invalid
    id) so the caller can log the transition.
    """
    try:
      row_id = uuid.UUID(signal_id)
    except (TypeError, ValueError):
      log.error("record_attempt_failure: invalid signal_id=%r", signal_id)
      return None

    try:
      async with get_session() as session:
        result = await session.execute(select(Signal).where(Signal.id == row_id))
        row: Optional[Signal] = result.scalars().first()
        if row is None:
          log.warning("record_attempt_failure: signal_id=%s not found", signal_id)
          return None
        row.last_attempt = datetime.now(timezone.utc)
        if row.attempts <= 1:
          row.attempts = 0
          row.status = SignalStatusEnum.FAILED
        else:
          row.attempts -= 1
        await session.flush()
        await session.refresh(row)
      log.info(
        "signal attempt failure id=%s attempts=%d status=%s",
        signal_id,
        row.attempts,
        row.status,
      )
      return row
    except Exception as exc:
      log.exception(
        "Failed to record attempt failure id=%s: %s", signal_id, exc
      )
      return None

  async def list_retryable(self, retry_interval_seconds: int) -> list[Signal]:
    """Return ``QUEUED`` signals eligible for another attempt, oldest first.

    A row is eligible when it is still ``QUEUED``, has attempts remaining, and
    either has never been attempted (``last_attempt IS NULL``) or its last
    attempt is older than ``retry_interval_seconds`` — the same interval the
    retry job polls at, so a row that just failed is not re-picked before the
    next tick.
    """
    threshold = datetime.now(timezone.utc) - timedelta(
      seconds=retry_interval_seconds
    )
    try:
      async with get_session() as session:
        result = await session.execute(
          select(Signal)
          .where(Signal.status == SignalStatusEnum.QUEUED)
          .where(Signal.attempts > 0)
          .where((Signal.last_attempt.is_(None)) | (Signal.last_attempt < threshold))
          .order_by(Signal.createdAt.asc())
        )
        return list(result.scalars().all())
    except Exception as exc:
      log.exception("Failed to list retryable signals: %s", exc)
      return []

  async def list_recent_by_strategies(
    self, strategies: list[str], since_seconds: int
  ) -> list[dict]:
    """Return raw webhook payloads for recent signals matching *strategies*.

    Backs the SYSTEM ``RETRY_SIGNALS`` replay a worker gets on connect: rows
    whose ``strategy`` is in *strategies* and whose ``createdAt`` is within the
    last *since_seconds*, newest first. Only the persisted ``raw`` JSON is
    returned so callers can feed it straight through ``parse_signal`` — the
    same code path the JetStream handler uses to produce the SIGNAL payload.
    """
    if not strategies or since_seconds <= 0:
      return []

    since_dt = datetime.now(timezone.utc) - timedelta(seconds=since_seconds)
    try:
      async with get_session() as session:
        result = await session.execute(
          select(Signal)
          .where(Signal.strategy.in_(strategies))
          .where(Signal.createdAt >= since_dt)
          .order_by(Signal.createdAt.asc())
        )
        rows = list(result.scalars().all())
    except Exception as exc:
      log.exception("Failed to list recent signals: %s", exc)
      return []

    envelopes: list[dict] = []
    for row in rows:
      raw = row.raw
      if not raw:
        continue
      envelopes.append(
        {
          "signal_id": str(row.id),
          "payload": raw,
        }
      )
    return envelopes


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
          market_type=MarketTypeEnum(event.market_type),
          gateway=event.gateway,
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
