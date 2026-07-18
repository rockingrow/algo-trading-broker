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

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from broker.db.engine import get_session
from broker.db.models import Account, BrokerSetting, Signal, TelegramSession, Trade
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

    Scoped by ``account_id`` + ``market`` (not ``account_id`` alone — that no
    longer identifies a single row, see ``uq_accounts_market_gateway_account_id``),
    matching either the exact gateway already on file or a legacy row whose
    gateway is still NULL (predates this column, being backfilled now).
    """
    try:
      async with get_session() as session:
        result = await session.execute(
          select(Account).where(
            Account.account_id == account_id,
            Account.market_type == market,
            or_(Account.gateway == gateway, Account.gateway.is_(None)),
          )
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

  async def create_account(
    self,
    account_id: str,
    market: MarketTypeEnum,
    gateway: str,
    account_name: Optional[str] = None,
  ) -> Optional[Account]:
    """Manually register a new account (admin-initiated, ahead of any
    trade/handshake). Returns None if the (market, gateway, account_id) triple
    is already taken — the same bare account_id may exist under a different
    market/gateway (see ``uq_accounts_market_gateway_account_id``).
    """
    try:
      async with get_session() as session:
        result = await session.execute(
          select(Account).where(
            Account.account_id == account_id,
            Account.market_type == market,
            Account.gateway == gateway,
          )
        )
        if result.scalars().first() is not None:
          return None

        account = Account(
          id=uuid.uuid4(),
          account_id=account_id,
          account_name=account_name,
          market_type=market,
          gateway=gateway,
          last_activity_at=datetime.now(timezone.utc),
        )
        session.add(account)
        await session.flush()
        await session.refresh(account)
        log.info(
          "Account created account_id=%s market=%s gateway=%s",
          account_id,
          market.value if isinstance(market, MarketTypeEnum) else market,
          gateway,
        )
        return account
    except Exception as exc:
      log.exception("Failed to create account_id=%s: %s", account_id, exc)
      return None

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

  async def list_by_telegram_user_id(self, telegram_user_id: int) -> list[Account]:
    """Return every account linked to a Telegram user (possibly several —
    ``telegram_user_id`` is not unique on ``accounts``, see
    ``TelegramSession`` for which one is currently active), ordered
    ``last_activity_at`` desc with ``createdAt`` asc as a deterministic
    tie-break (two admin-created accounts that haven't traded yet both have
    ``last_activity_at IS NULL``)."""
    try:
      async with get_session() as session:
        result = await session.execute(
          select(Account)
          .where(Account.telegram_user_id == telegram_user_id)
          .order_by(Account.last_activity_at.desc(), Account.createdAt.asc())
        )
        return list(result.scalars().all())
    except Exception as exc:
      log.exception(
        "Failed to fetch accounts for telegram_user_id=%s: %s",
        telegram_user_id,
        exc,
      )
      return []

  async def get_active_account(self, telegram_user_id: int) -> Account | None:
    """Return the Telegram user's currently active account, or None if they
    have no linked accounts at all.

    Reads ``TelegramSession.active_account_id`` and validates it still
    belongs to this user in one query (covers a stale pointer left over from
    ``unlink_telegram``). Falls back to the first row from
    ``list_by_telegram_user_id`` when the session is missing, its pointer is
    NULL, or stale — self-healing the session best-effort so the next call
    hits the fast path (failure to self-heal is logged and non-fatal; the
    correct fallback account is still returned).
    """
    try:
      async with get_session() as session:
        result = await session.execute(
          select(TelegramSession).where(
            TelegramSession.telegram_user_id == telegram_user_id
          )
        )
        tg_session: Optional[TelegramSession] = result.scalars().first()

        if tg_session is not None and tg_session.active_account_id is not None:
          result = await session.execute(
            select(Account).where(
              Account.id == tg_session.active_account_id,
              Account.telegram_user_id == telegram_user_id,
            )
          )
          account: Optional[Account] = result.scalars().first()
          if account is not None:
            return account
    except Exception as exc:
      log.exception(
        "Failed to fetch active account for telegram_user_id=%s: %s",
        telegram_user_id,
        exc,
      )
      return None

    accounts = await self.list_by_telegram_user_id(telegram_user_id)
    if not accounts:
      return None
    fallback = accounts[0]

    try:
      async with get_session() as session:
        result = await session.execute(
          select(TelegramSession).where(
            TelegramSession.telegram_user_id == telegram_user_id
          )
        )
        tg_session = result.scalars().first()
        if tg_session is None:
          session.add(
            TelegramSession(
              id=uuid.uuid4(),
              telegram_user_id=telegram_user_id,
              active_account_id=fallback.id,
            )
          )
        else:
          tg_session.active_account_id = fallback.id
    except Exception as exc:
      log.warning(
        "Failed to self-heal active session for telegram_user_id=%s: %s",
        telegram_user_id,
        exc,
      )

    return fallback

  async def set_active_account(
    self, telegram_user_id: int, account_id: uuid.UUID
  ) -> Account | None:
    """Set the Telegram user's active account. Returns the account, or None
    if no account with that id is linked to this ``telegram_user_id``.

    The ownership check (``id == account_id AND telegram_user_id ==
    telegram_user_id``) is deliberate: unlike the rest of this class, which
    treats ``telegram_user_id`` as a trusted, bot-verified identity,
    ``account_id`` here is client-supplied and must be checked against it —
    a "not found" and a "found but not yours" both return None so no
    information about other users' account ids leaks.
    """
    try:
      async with get_session() as session:
        result = await session.execute(
          select(Account).where(
            Account.id == account_id, Account.telegram_user_id == telegram_user_id
          )
        )
        account: Optional[Account] = result.scalars().first()
        if account is None:
          return None

        result = await session.execute(
          select(TelegramSession).where(
            TelegramSession.telegram_user_id == telegram_user_id
          )
        )
        tg_session: Optional[TelegramSession] = result.scalars().first()
        if tg_session is None:
          session.add(
            TelegramSession(
              id=uuid.uuid4(),
              telegram_user_id=telegram_user_id,
              active_account_id=account.id,
            )
          )
        else:
          tg_session.active_account_id = account.id

        await session.flush()
        await session.refresh(account)
        log.info(
          "Active account set telegram_user_id=%s account_id=%s",
          telegram_user_id,
          account.account_id,
        )
        return account
    except Exception as exc:
      log.exception(
        "Failed to set active account for telegram_user_id=%s account_id=%s: %s",
        telegram_user_id,
        account_id,
        exc,
      )
      return None

  async def link_telegram(
    self, token: uuid.UUID, telegram_user_id: int
  ) -> Account | None:
    """Bind ``telegram_user_id`` to the account identified by ``token``.

    A Telegram user may hold several linked accounts, so this no longer
    releases any prior binding. If the user's ``TelegramSession`` is missing
    or has no active account yet, the newly linked account becomes active
    (covers both "first ever link" and "relinking after a full unlink").
    Otherwise an existing active selection is left untouched — adding a 2nd
    or 3rd account doesn't disturb which one is currently active. Returns the
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

        account.telegram_user_id = telegram_user_id

        result = await session.execute(
          select(TelegramSession).where(
            TelegramSession.telegram_user_id == telegram_user_id
          )
        )
        tg_session: Optional[TelegramSession] = result.scalars().first()
        if tg_session is None:
          session.add(
            TelegramSession(
              id=uuid.uuid4(),
              telegram_user_id=telegram_user_id,
              active_account_id=account.id,
            )
          )
        elif tg_session.active_account_id is None:
          tg_session.active_account_id = account.id

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
    """Clear the Telegram binding for the user's *active* account, and
    re-point the active selection at another remaining linked account (if
    any) or clear it. Returns True if a row changed.

    Deliberately calls ``get_active_account`` (its own ``get_session()``
    block) rather than duplicating the active-resolution + fallback-ordering
    logic here — an extra round trip on this low-frequency, user-initiated
    action, in exchange for one source of truth for "what's active."
    """
    active = await self.get_active_account(telegram_user_id)
    if active is None:
      return False

    try:
      async with get_session() as session:
        result = await session.execute(select(Account).where(Account.id == active.id))
        account: Optional[Account] = result.scalars().first()
        if account is None:
          return False
        account.telegram_user_id = None
        await session.flush()

        result = await session.execute(
          select(Account)
          .where(Account.telegram_user_id == telegram_user_id, Account.id != active.id)
          .order_by(Account.last_activity_at.desc(), Account.createdAt.asc())
        )
        remaining: Optional[Account] = result.scalars().first()

        result = await session.execute(
          select(TelegramSession).where(
            TelegramSession.telegram_user_id == telegram_user_id
          )
        )
        tg_session: Optional[TelegramSession] = result.scalars().first()
        if tg_session is not None:
          tg_session.active_account_id = remaining.id if remaining is not None else None

        log.info(
          "Unlinked telegram_user_id=%s from account_id=%s",
          telegram_user_id,
          account.account_id,
        )
        return True
    except Exception as exc:
      log.exception("Failed to unlink telegram_user_id=%s: %s", telegram_user_id, exc)
      return False

  async def rotate_link_token(self, account_id: str) -> uuid.UUID | None:
    """Issue a fresh link token for an account (revokes the old one).

    Returns the new token, or None if the account does not exist.

    KNOWN LIMITATION: resolves by bare ``account_id`` alone, which is no
    longer guaranteed unique (see ``uq_accounts_market_gateway_account_id`` —
    the same id can exist under different market/gateway pairs). If it
    collides, this rotates whichever matching row Postgres returns first,
    non-deterministically. Not fixed here: doing so needs the admin-facing
    callers (bot ``/rotate``, ``POST /admin/accounts/{account_id}/...``) to
    also pass market_type + gateway, which is a wider UX change than this
    schema migration. Avoid reusing account_id across gateways until then.
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
    last_activity_at.

    Scoped by account_id + market_type (see ``upsert_gateway`` for why
    account_id alone isn't enough), matching either the exact gateway already
    on file or a legacy row whose gateway is still NULL.
    """
    market_type = MarketTypeEnum(event.market_type)
    result = await session.execute(
      select(Account).where(
        Account.account_id == event.account_id,
        Account.market_type == market_type,
        or_(Account.gateway == event.gateway, Account.gateway.is_(None)),
      )
    )
    row: Optional[Account] = result.scalars().first()
    now = datetime.now(timezone.utc)

    if row is not None:
      if event.account_name is not None:
        row.account_name = event.account_name
      row.market_type = market_type
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
          market_type=market_type,
          gateway=event.gateway,
          last_activity_at=now,
        )
      )

  async def upsert_by_position_event(self, event: PositionEvent) -> Trade | None:
    """Apply a PositionEvent received from the worker (via NATS TRADE) to the
    broker's `trades` table. Performs an upsert keyed by (market_type, gateway,
    account_id, ref_id) — account_id alone doesn't identify an account
    uniquely (see ``uq_accounts_market_gateway_account_id``); updates the row
    if it exists, otherwise inserts a new one. Idempotent."""
    trade_status = self._policy.to_trade_status(event.status)
    if trade_status is None:
      log.warning("upsert_by_position_event: unknown position status=%s", event.status)
      return None

    is_running = self._policy.is_open(event.status)
    price = event.closed_price if event.closed_price is not None else event.opened_price
    market_type = MarketTypeEnum(event.market_type)

    async with get_session() as session:
      await self._upsert_account(session, event)

      result = await session.execute(
        select(Trade).where(
          Trade.account_id == event.account_id,
          Trade.ref_id == event.ref_source_id,
          Trade.market_type == market_type,
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
        row.is_running = is_running
        row.price = price
        row.quantity = event.volume
        row.market_type = market_type
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
        market_type=market_type,
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
    (see ``rotate_link_token``'s docstring). Not scoped to market_type/gateway
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
    """Return the total number of trades for an account.

    Same bare-``account_id`` limitation as ``list_by_account``.
    """
    try:
      async with get_session() as session:
        result = await session.execute(
          select(func.count()).select_from(Trade).where(Trade.account_id == account_id)
        )
        return result.scalar_one()
    except Exception as exc:
      log.exception("Failed to count trades for account_id=%s: %s", account_id, exc)
      return 0
