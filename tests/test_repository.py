"""Unit tests for the SQLAlchemy repositories.

A real Postgres engine isn't available in CI, so ``broker.db.repository.get_session``
is monkeypatched with an in-memory fake session. The fakes implement just
enough of the SQLAlchemy AsyncSession surface (execute/add/flush/refresh) for
the repository code paths under test — these tests target the repository's
own logic (field mapping, lifecycle rules), not SQLAlchemy itself.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone


from broker.db import repository as repo_mod
from broker.db.models import Account, BrokerSetting, TelegramSession, Trade
from broker.db.repository import (
  SqlAlchemyAccountRepository,
  SqlAlchemySettingRepository,
  SqlAlchemySignalRepository,
  SqlAlchemyTradeRepository,
)
from broker.schemas.core import SignalActionEnum
from broker.schemas.trade_event_schema import PositionEvent
from broker.schemas.trade_schema import TradeStatusEnum
from broker.schemas.account_schema import MarketTypeEnum
from broker.schemas.webhook_schema import InputsSchema, PositionSchema, WebhookPayload


# ── Fake session machinery ──────────────────────────────────────────


class _Result:
  def __init__(self, rows):
    self._rows = rows

  def scalars(self):
    return self

  def first(self):
    return self._rows[0] if self._rows else None

  def all(self):
    return list(self._rows)


class FakeSession:
  """Minimal AsyncSession stand-in. ``execute`` returns rows from a queue of
  pre-seeded results (one per execute call, FIFO)."""

  def __init__(self, results: list[list]):
    self._results = list(results)
    self.added = []
    self.flushed = 0
    self.refreshed = []

  async def execute(self, _stmt):
    rows = self._results.pop(0) if self._results else []
    return _Result(rows)

  def add(self, obj):
    self.added.append(obj)

  async def flush(self):
    self.flushed += 1

  async def refresh(self, obj):
    self.refreshed.append(obj)


def _patch_session(monkeypatch, session: FakeSession):
  @contextlib.asynccontextmanager
  async def fake_get_session():
    yield session

  monkeypatch.setattr(repo_mod, "get_session", fake_get_session)


# ── SignalRepository.log_signal ─────────────────────────────────────


def _payload(**overrides) -> WebhookPayload:
  base = dict(
    strategy="strat",
    symbol="OANDA:XAUUSD",
    timeframe="60",
    timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
    position=PositionSchema(action=SignalActionEnum.LONG, price=100.0, quantity=1.0),
    token="t",
  )
  base.update(overrides)
  return WebhookPayload(**base)


async def test_log_signal_persists_row_and_returns_id(monkeypatch):
  session = FakeSession(results=[])
  _patch_session(monkeypatch, session)

  result = await SqlAlchemySignalRepository().log_signal(_payload())

  assert result is not None
  assert len(session.added) == 1
  row = session.added[0]
  assert row.symbol == "OANDA:XAUUSD"
  assert row.action == SignalActionEnum.LONG
  assert str(row.id) == result


async def test_log_signal_risk_percent_prefers_position(monkeypatch):
  """Regression: persisted risk_percent must match the published signal,
  taking the position-level value over inputs."""
  session = FakeSession(results=[])
  _patch_session(monkeypatch, session)

  payload = _payload(
    position=PositionSchema(
      action=SignalActionEnum.LONG, price=1, quantity=1, risk_percent=2.5
    ),
    inputs=InputsSchema(risk_percent=9.9),
  )
  await SqlAlchemySignalRepository().log_signal(payload)
  assert session.added[0].risk_percent == 2.5


async def test_log_signal_risk_percent_falls_back_to_inputs(monkeypatch):
  session = FakeSession(results=[])
  _patch_session(monkeypatch, session)

  payload = _payload(
    position=PositionSchema(action=SignalActionEnum.LONG, price=1, quantity=1),
    inputs=InputsSchema(risk_percent=3.3),
  )
  await SqlAlchemySignalRepository().log_signal(payload)
  assert session.added[0].risk_percent == 3.3


async def test_log_signal_risk_percent_defaults_zero(monkeypatch):
  session = FakeSession(results=[])
  _patch_session(monkeypatch, session)

  await SqlAlchemySignalRepository().log_signal(_payload())
  assert session.added[0].risk_percent == 0.0


async def test_log_signal_returns_none_on_error(monkeypatch):
  class BoomSession(FakeSession):
    def add(self, obj):
      raise RuntimeError("db down")

  _patch_session(monkeypatch, BoomSession(results=[]))
  result = await SqlAlchemySignalRepository().log_signal(_payload())
  assert result is None


async def test_log_signal_seeds_status_queued_and_max_attempts(monkeypatch):
  from broker.schemas.core import SignalStatusEnum
  from broker.settings import settings

  session = FakeSession(results=[])
  _patch_session(monkeypatch, session)

  await SqlAlchemySignalRepository().log_signal(_payload())
  row = session.added[0]
  assert row.status == SignalStatusEnum.QUEUED
  assert row.attempts == settings.SIGNAL_MAX_ATTEMPTS
  assert row.last_attempt is None


# ── SignalRepository.record_attempt_failure ─────────────────────────


class _SignalRow:
  def __init__(self, attempts: int, status):
    import uuid as _uuid

    self.id = _uuid.uuid4()
    self.attempts = attempts
    self.status = status
    self.last_attempt = None


async def test_record_attempt_failure_decrements_and_stamps_time(monkeypatch):
  from broker.schemas.core import SignalStatusEnum

  row = _SignalRow(attempts=3, status=SignalStatusEnum.QUEUED)
  session = FakeSession(results=[[row]])
  _patch_session(monkeypatch, session)

  updated = await SqlAlchemySignalRepository().record_attempt_failure(str(row.id))
  assert updated is not None
  assert updated.attempts == 2
  assert updated.status == SignalStatusEnum.QUEUED
  assert updated.last_attempt is not None


async def test_record_attempt_failure_flips_to_failed_on_last_attempt(monkeypatch):
  from broker.schemas.core import SignalStatusEnum

  row = _SignalRow(attempts=1, status=SignalStatusEnum.QUEUED)
  session = FakeSession(results=[[row]])
  _patch_session(monkeypatch, session)

  updated = await SqlAlchemySignalRepository().record_attempt_failure(str(row.id))
  assert updated is not None
  assert updated.attempts == 0
  assert updated.status == SignalStatusEnum.FAILED


async def test_record_attempt_failure_missing_row_returns_none(monkeypatch):
  session = FakeSession(results=[[]])
  _patch_session(monkeypatch, session)
  import uuid as _uuid

  assert (
    await SqlAlchemySignalRepository().record_attempt_failure(str(_uuid.uuid4()))
    is None
  )


async def test_record_attempt_failure_rejects_bad_id():
  assert (
    await SqlAlchemySignalRepository().record_attempt_failure("not-a-uuid") is None
  )


async def test_list_retryable_returns_rows(monkeypatch):
  from broker.schemas.core import SignalStatusEnum

  row = _SignalRow(attempts=2, status=SignalStatusEnum.QUEUED)
  session = FakeSession(results=[[row]])
  _patch_session(monkeypatch, session)

  rows = await SqlAlchemySignalRepository().list_retryable(15)
  assert rows == [row]


async def test_list_retryable_swallows_db_error(monkeypatch):
  class BoomSession(FakeSession):
    async def execute(self, _stmt):
      raise RuntimeError("db down")

  _patch_session(monkeypatch, BoomSession(results=[]))
  assert await SqlAlchemySignalRepository().list_retryable(15) == []


# ── TradeRepository.upsert_by_position_event ────────────────────────


def _event(**overrides) -> PositionEvent:
  base = dict(
    event="CREATED",
    market_type="FOREX",
    strategy="strat",
    id=1,
    ref_source_id="rs-1",
    ref_id="r-1",
    symbol="XAUUSD",
    action="long",
    volume=0.1,
    opened_price=100.0,
    status="OPENED",
    account_id="acc-1",
    account_balance=1000.0,
  )
  base.update(overrides)
  return PositionEvent(**base)


async def test_upsert_unknown_status_returns_none(monkeypatch):
  session = FakeSession(results=[])
  _patch_session(monkeypatch, session)
  result = await SqlAlchemyTradeRepository().upsert_by_position_event(
    _event(status="WAT")
  )
  assert result is None


async def test_upsert_inserts_new_trade_and_account(monkeypatch):
  # execute #1: account lookup (none) -> insert account
  # execute #2: trade lookup (none) -> insert trade
  session = FakeSession(results=[[], []])
  _patch_session(monkeypatch, session)

  result = await SqlAlchemyTradeRepository().upsert_by_position_event(_event())

  assert isinstance(result, Trade)
  assert result.account_id == "acc-1"
  assert result.action == "LONG"  # upper-cased
  assert result.status == TradeStatusEnum.OPENED
  assert result.is_running is True
  assert result.price == 100.0  # opened_price when no closed_price
  # both an Account and a Trade were added
  assert any(isinstance(o, Account) for o in session.added)
  assert any(isinstance(o, Trade) for o in session.added)


async def test_upsert_persists_gateway_on_new_account(monkeypatch):
  session = FakeSession(results=[[], []])
  _patch_session(monkeypatch, session)

  await SqlAlchemyTradeRepository().upsert_by_position_event(
    _event(market_type="CRYPTO", gateway="BINANCE")
  )

  account = next(o for o in session.added if isinstance(o, Account))
  assert account.gateway == "BINANCE"
  assert account.market_type == MarketTypeEnum.CRYPTO


async def test_upsert_updates_gateway_on_existing_account(monkeypatch):
  existing_account = Account(
    account_id="acc-1", market_type=MarketTypeEnum.CRYPTO, gateway="OLD"
  )
  existing_trade = Trade(
    account_id="acc-1",
    ref_id="rs-1",
    strategy="strat",
    strategy_code="",
    symbol="XAUUSD",
    action="LONG",
    price=100.0,
    quantity=0.1,
    is_running=True,
    risk_percent=1.0,
    status=TradeStatusEnum.OPENED,
  )
  session = FakeSession(results=[[existing_account], [existing_trade]])
  _patch_session(monkeypatch, session)

  await SqlAlchemyTradeRepository().upsert_by_position_event(
    _event(status="TP1", gateway="BINANCE")
  )
  assert existing_account.gateway == "BINANCE"


async def test_upsert_uses_closed_price_when_present(monkeypatch):
  session = FakeSession(results=[[], []])
  _patch_session(monkeypatch, session)
  result = await SqlAlchemyTradeRepository().upsert_by_position_event(
    _event(status="TP2", closed_price=120.0)
  )
  assert result.price == 120.0
  assert result.status == TradeStatusEnum.CLOSED
  assert result.is_running is False


async def test_upsert_inserts_rejected_trade_with_reason(monkeypatch):
  # A worker that hits its MAX ORDER limit still persists the order and fires a
  # TRADE with status REJECTED; the broker records it as a terminal, non-running
  # trade carrying the reject reason.
  session = FakeSession(results=[[], []])
  _patch_session(monkeypatch, session)

  result = await SqlAlchemyTradeRepository().upsert_by_position_event(
    _event(status="REJECTED", reject_reason="MAX ORDER limit reached")
  )

  assert isinstance(result, Trade)
  assert result.status == TradeStatusEnum.REJECTED
  assert result.is_running is False
  assert result.reject_reason == "MAX ORDER limit reached"


async def test_upsert_updates_existing_trade(monkeypatch):
  existing_account = Account(account_id="acc-1", market_type=MarketTypeEnum.FOREX)
  existing_trade = Trade(
    account_id="acc-1",
    ref_id="rs-1",
    strategy="strat",
    strategy_code="",
    symbol="XAUUSD",
    action="LONG",
    price=100.0,
    quantity=0.1,
    is_running=True,
    risk_percent=1.0,
    status=TradeStatusEnum.OPENED,
  )
  session = FakeSession(results=[[existing_account], [existing_trade]])
  _patch_session(monkeypatch, session)

  result = await SqlAlchemyTradeRepository().upsert_by_position_event(
    _event(status="TP1", volume=0.2)
  )
  assert result is existing_trade
  assert result.status == TradeStatusEnum.PARTIALLY_CLOSED
  assert result.quantity == 0.2
  assert result.is_running is True


async def test_upsert_ignores_status_downgrade(monkeypatch):
  existing_account = Account(account_id="acc-1", market_type=MarketTypeEnum.FOREX)
  existing_trade = Trade(
    account_id="acc-1",
    ref_id="rs-1",
    strategy="strat",
    strategy_code="",
    symbol="XAUUSD",
    action="LONG",
    price=120.0,
    quantity=0.1,
    is_running=False,
    risk_percent=1.0,
    status=TradeStatusEnum.CLOSED,
  )
  session = FakeSession(results=[[existing_account], [existing_trade]])
  _patch_session(monkeypatch, session)

  # CLOSED -> OPENED is a downgrade; the row must be returned unchanged.
  result = await SqlAlchemyTradeRepository().upsert_by_position_event(
    _event(status="OPENED")
  )
  assert result is existing_trade
  assert result.status == TradeStatusEnum.CLOSED
  assert result.is_running is False


# ── TradeRepository list/count error handling ───────────────────────


async def test_list_by_account_returns_empty_on_error(monkeypatch):
  class BoomSession(FakeSession):
    async def execute(self, _stmt):
      raise RuntimeError("db down")

  _patch_session(monkeypatch, BoomSession(results=[]))
  result = await SqlAlchemyTradeRepository().list_by_account(
    "acc-1", limit=10, offset=0
  )
  assert result == []


async def test_count_by_account_returns_zero_on_error(monkeypatch):
  class BoomSession(FakeSession):
    async def execute(self, _stmt):
      raise RuntimeError("db down")

  _patch_session(monkeypatch, BoomSession(results=[]))
  result = await SqlAlchemyTradeRepository().count_by_account("acc-1")
  assert result == 0


# ── SettingRepository.get_many ───────────────────────────────────────


def _setting(key: str, value: str) -> BrokerSetting:
  return BrokerSetting(key=key, value=value)


async def test_get_many_returns_found_keys(monkeypatch):
  session = FakeSession(results=[[_setting("a", "1"), _setting("b", "2")]])
  _patch_session(monkeypatch, session)

  result = await SqlAlchemySettingRepository().get_many(["a", "b"])
  assert result == {"a": "1", "b": "2"}


async def test_get_many_omits_missing_keys(monkeypatch):
  # Only "a" has a row; the query legitimately returns just that one.
  session = FakeSession(results=[[_setting("a", "1")]])
  _patch_session(monkeypatch, session)

  result = await SqlAlchemySettingRepository().get_many(["a", "b"])
  assert result == {"a": "1"}


async def test_get_many_returns_empty_dict_on_error(monkeypatch):
  class BoomSession(FakeSession):
    async def execute(self, _stmt):
      raise RuntimeError("db down")

  _patch_session(monkeypatch, BoomSession(results=[]))
  result = await SqlAlchemySettingRepository().get_many(["a", "b"])
  assert result == {}


# ── AccountRepository.upsert_gateway (WORKER_CONNECTED handshake) ─────


async def test_upsert_gateway_backfills_existing_account(monkeypatch):
  # The row predates the gateway column (or has only ever seen gateway-less
  # TRADE events), so the handshake is what fills it in.
  existing = Account(
    account_id="acc-1", market_type=MarketTypeEnum.CRYPTO, gateway=None
  )
  session = FakeSession(results=[[existing]])
  _patch_session(monkeypatch, session)

  await SqlAlchemyAccountRepository().upsert_gateway(
    "acc-1", MarketTypeEnum.CRYPTO, "BINANCE"
  )

  assert existing.gateway == "BINANCE"
  assert session.added == []


async def test_upsert_gateway_inserts_unknown_account(monkeypatch):
  # A worker that has connected but never traded is still addressable.
  session = FakeSession(results=[[]])
  _patch_session(monkeypatch, session)

  await SqlAlchemyAccountRepository().upsert_gateway(
    "acc-2", MarketTypeEnum.FOREX, "MT5"
  )

  assert len(session.added) == 1
  row = session.added[0]
  assert row.account_id == "acc-2"
  assert row.market_type == MarketTypeEnum.FOREX
  assert row.gateway == "MT5"
  assert row.last_activity_at is not None


async def test_upsert_gateway_is_noop_when_unchanged(monkeypatch):
  existing = Account(
    account_id="acc-1", market_type=MarketTypeEnum.CRYPTO, gateway="BINANCE"
  )
  session = FakeSession(results=[[existing]])
  _patch_session(monkeypatch, session)

  await SqlAlchemyAccountRepository().upsert_gateway(
    "acc-1", MarketTypeEnum.CRYPTO, "BINANCE"
  )

  assert existing.gateway == "BINANCE"
  assert session.added == []


async def test_upsert_gateway_swallows_db_error(monkeypatch):
  class BoomSession(FakeSession):
    async def execute(self, _stmt):
      raise RuntimeError("db down")

  _patch_session(monkeypatch, BoomSession(results=[]))
  # Bookkeeping only — a DB failure must never break the handshake reply.
  await SqlAlchemyAccountRepository().upsert_gateway(
    "acc-1", MarketTypeEnum.CRYPTO, "BINANCE"
  )


# ── AccountRepository.create_account (admin manual registration) ──────


async def test_create_account_inserts_new_row(monkeypatch):
  session = FakeSession(results=[[]])
  _patch_session(monkeypatch, session)

  account = await SqlAlchemyAccountRepository().create_account(
    "7654321", MarketTypeEnum.CRYPTO, "BINANCE", "Main Crypto"
  )

  assert len(session.added) == 1
  row = session.added[0]
  assert row.account_id == "7654321"
  assert row.market_type == MarketTypeEnum.CRYPTO
  assert row.gateway == "BINANCE"
  assert row.account_name == "Main Crypto"
  assert row.last_activity_at is not None
  assert account is row
  assert session.refreshed == [row]


async def test_create_account_returns_none_when_account_id_taken(monkeypatch):
  # Same (market_type, gateway, account_id) triple already exists.
  existing = Account(
    account_id="7654321", market_type=MarketTypeEnum.CRYPTO, gateway="BINANCE"
  )
  session = FakeSession(results=[[existing]])
  _patch_session(monkeypatch, session)

  account = await SqlAlchemyAccountRepository().create_account(
    "7654321", MarketTypeEnum.CRYPTO, "BINANCE"
  )

  assert account is None
  assert session.added == []


async def test_create_account_swallows_db_error(monkeypatch):
  class BoomSession(FakeSession):
    async def execute(self, _stmt):
      raise RuntimeError("db down")

  _patch_session(monkeypatch, BoomSession(results=[]))
  account = await SqlAlchemyAccountRepository().create_account(
    "7654321", MarketTypeEnum.CRYPTO, "BINANCE"
  )
  assert account is None


# ── AccountRepository telegram binding ──────────────────────────────


import uuid  # noqa: E402


async def test_link_telegram_binds_user_and_activates_first_account(monkeypatch):
  token = uuid.uuid4()
  account = Account(
    id=uuid.uuid4(),
    account_id="acc-1",
    market_type=MarketTypeEnum.FOREX,
    telegram_link_token=token,
  )
  # 1st execute: lookup by token → account; 2nd: session lookup → none → insert
  session = FakeSession(results=[[account], []])
  _patch_session(monkeypatch, session)

  result = await SqlAlchemyAccountRepository().link_telegram(token, 555)
  assert result is account
  assert account.telegram_user_id == 555
  new_session = next(o for o in session.added if isinstance(o, TelegramSession))
  assert new_session.telegram_user_id == 555
  assert new_session.active_account_id == account.id


async def test_link_telegram_does_not_release_previous_account(monkeypatch):
  """Multi-account support: linking a 2nd account must NOT clear the 1st's
  telegram_user_id (replaces the old 1:1 "latest claim wins" behavior)."""
  token = uuid.uuid4()
  target = Account(
    id=uuid.uuid4(),
    account_id="acc-2",
    market_type=MarketTypeEnum.FOREX,
    telegram_link_token=token,
  )
  previous = Account(
    id=uuid.uuid4(),
    account_id="acc-1",
    market_type=MarketTypeEnum.FOREX,
    telegram_user_id=555,
  )
  existing_session = TelegramSession(
    telegram_user_id=555, active_account_id=previous.id
  )
  # 1st execute: lookup by token → target; 2nd: session lookup → existing, active
  session = FakeSession(results=[[target], [existing_session]])
  _patch_session(monkeypatch, session)

  result = await SqlAlchemyAccountRepository().link_telegram(token, 555)
  assert result is target
  assert target.telegram_user_id == 555
  assert previous.telegram_user_id == 555  # untouched
  assert existing_session.active_account_id == previous.id  # active unchanged


async def test_link_telegram_activates_when_session_has_no_active(monkeypatch):
  token = uuid.uuid4()
  account = Account(
    id=uuid.uuid4(),
    account_id="acc-1",
    market_type=MarketTypeEnum.FOREX,
    telegram_link_token=token,
  )
  # Session row exists (e.g. after a full unlink) but has no active account.
  empty_session = TelegramSession(telegram_user_id=555, active_account_id=None)
  session = FakeSession(results=[[account], [empty_session]])
  _patch_session(monkeypatch, session)

  await SqlAlchemyAccountRepository().link_telegram(token, 555)
  assert empty_session.active_account_id == account.id


async def test_link_telegram_invalid_token_returns_none(monkeypatch):
  session = FakeSession(results=[[]])
  _patch_session(monkeypatch, session)

  result = await SqlAlchemyAccountRepository().link_telegram(uuid.uuid4(), 555)
  assert result is None


# ── AccountRepository.list_by_telegram_user_id / get_active_account /
#    set_active_account ────────────────────────────────────────────


async def test_list_by_telegram_user_id_returns_rows(monkeypatch):
  a1 = Account(account_id="acc-1", market_type=MarketTypeEnum.FOREX, telegram_user_id=555)
  a2 = Account(account_id="acc-2", market_type=MarketTypeEnum.CRYPTO, telegram_user_id=555)
  session = FakeSession(results=[[a1, a2]])
  _patch_session(monkeypatch, session)

  rows = await SqlAlchemyAccountRepository().list_by_telegram_user_id(555)
  assert rows == [a1, a2]


async def test_get_active_account_fast_path(monkeypatch):
  account = Account(
    id=uuid.uuid4(), account_id="acc-1", market_type=MarketTypeEnum.FOREX, telegram_user_id=555
  )
  tg_session = TelegramSession(telegram_user_id=555, active_account_id=account.id)
  session = FakeSession(results=[[tg_session], [account]])
  _patch_session(monkeypatch, session)

  result = await SqlAlchemyAccountRepository().get_active_account(555)
  assert result is account


async def test_get_active_account_falls_back_and_self_heals(monkeypatch):
  # No session row at all → fall back to the first linked account (its own
  # list_by_telegram_user_id call) and self-heal a new session row.
  account = Account(
    id=uuid.uuid4(), account_id="acc-1", market_type=MarketTypeEnum.FOREX, telegram_user_id=555
  )
  session = FakeSession(results=[[], [account], []])
  _patch_session(monkeypatch, session)

  result = await SqlAlchemyAccountRepository().get_active_account(555)
  assert result is account
  healed = next(o for o in session.added if isinstance(o, TelegramSession))
  assert healed.active_account_id == account.id


async def test_get_active_account_returns_none_when_unlinked(monkeypatch):
  session = FakeSession(results=[[], []])
  _patch_session(monkeypatch, session)

  result = await SqlAlchemyAccountRepository().get_active_account(555)
  assert result is None
  assert session.added == []


async def test_set_active_account_owned(monkeypatch):
  account = Account(
    id=uuid.uuid4(), account_id="acc-2", market_type=MarketTypeEnum.CRYPTO, telegram_user_id=555
  )
  tg_session = TelegramSession(telegram_user_id=555, active_account_id=uuid.uuid4())
  session = FakeSession(results=[[account], [tg_session]])
  _patch_session(monkeypatch, session)

  result = await SqlAlchemyAccountRepository().set_active_account(555, account.id)
  assert result is account
  assert tg_session.active_account_id == account.id


async def test_set_active_account_not_owned_returns_none(monkeypatch):
  session = FakeSession(results=[[]])
  _patch_session(monkeypatch, session)

  result = await SqlAlchemyAccountRepository().set_active_account(555, uuid.uuid4())
  assert result is None


# ── AccountRepository.unlink_telegram ───────────────────────────────


async def test_unlink_telegram_clears_active_and_reassigns(monkeypatch):
  active = Account(
    id=uuid.uuid4(), account_id="acc-1", market_type=MarketTypeEnum.FOREX, telegram_user_id=555
  )
  remaining = Account(
    id=uuid.uuid4(), account_id="acc-2", market_type=MarketTypeEnum.CRYPTO, telegram_user_id=555
  )
  tg_session_a = TelegramSession(telegram_user_id=555, active_account_id=active.id)
  tg_session_b = TelegramSession(telegram_user_id=555, active_account_id=active.id)
  session = FakeSession(
    results=[
      [tg_session_a],  # get_active_account: session lookup
      [active],  # get_active_account: ownership-validated account lookup
      [active],  # unlink: re-fetch account by id
      [remaining],  # unlink: remaining linked accounts
      [tg_session_b],  # unlink: session lookup to re-point active
    ]
  )
  _patch_session(monkeypatch, session)

  ok = await SqlAlchemyAccountRepository().unlink_telegram(555)
  assert ok is True
  assert active.telegram_user_id is None
  assert tg_session_b.active_account_id == remaining.id


async def test_unlink_telegram_clears_session_when_last_account(monkeypatch):
  active = Account(
    id=uuid.uuid4(), account_id="acc-1", market_type=MarketTypeEnum.FOREX, telegram_user_id=555
  )
  tg_session_a = TelegramSession(telegram_user_id=555, active_account_id=active.id)
  tg_session_b = TelegramSession(telegram_user_id=555, active_account_id=active.id)
  session = FakeSession(
    results=[
      [tg_session_a],
      [active],
      [active],
      [],  # no remaining accounts
      [tg_session_b],
    ]
  )
  _patch_session(monkeypatch, session)

  ok = await SqlAlchemyAccountRepository().unlink_telegram(555)
  assert ok is True
  assert tg_session_b.active_account_id is None


async def test_unlink_telegram_no_account(monkeypatch):
  session = FakeSession(results=[[], []])  # get_active_account finds nothing
  _patch_session(monkeypatch, session)
  assert await SqlAlchemyAccountRepository().unlink_telegram(555) is False


async def test_rotate_link_token_issues_new_token(monkeypatch):
  old = uuid.uuid4()
  account = Account(
    account_id="acc-1", market_type=MarketTypeEnum.FOREX, telegram_link_token=old
  )
  session = FakeSession(results=[[account]])
  _patch_session(monkeypatch, session)

  new_token = await SqlAlchemyAccountRepository().rotate_link_token("acc-1")
  assert new_token is not None
  assert new_token != old
  assert account.telegram_link_token == new_token
