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
from broker.db.models import Account, Trade
from broker.db.repository import (
  SqlAlchemyAccountRepository,
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


async def test_upsert_uses_closed_price_when_present(monkeypatch):
  session = FakeSession(results=[[], []])
  _patch_session(monkeypatch, session)
  result = await SqlAlchemyTradeRepository().upsert_by_position_event(
    _event(status="TP2", closed_price=120.0)
  )
  assert result.price == 120.0
  assert result.status == TradeStatusEnum.CLOSED
  assert result.is_running is False


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


# ── AccountRepository telegram binding ──────────────────────────────


import uuid  # noqa: E402


async def test_link_telegram_binds_user(monkeypatch):
  token = uuid.uuid4()
  account = Account(
    account_id="acc-1", market_type=MarketTypeEnum.FOREX, telegram_link_token=token
  )
  # 1st execute: lookup by token → account; 2nd: other accounts with this tg id → none
  session = FakeSession(results=[[account], []])
  _patch_session(monkeypatch, session)

  result = await SqlAlchemyAccountRepository().link_telegram(token, 555)
  assert result is account
  assert account.telegram_user_id == 555


async def test_link_telegram_releases_previous_account(monkeypatch):
  token = uuid.uuid4()
  target = Account(
    account_id="acc-2", market_type=MarketTypeEnum.FOREX, telegram_link_token=token
  )
  previous = Account(
    account_id="acc-1", market_type=MarketTypeEnum.FOREX, telegram_user_id=555
  )
  session = FakeSession(results=[[target], [previous]])
  _patch_session(monkeypatch, session)

  result = await SqlAlchemyAccountRepository().link_telegram(token, 555)
  assert result is target
  assert target.telegram_user_id == 555
  assert previous.telegram_user_id is None  # latest claim wins


async def test_link_telegram_invalid_token_returns_none(monkeypatch):
  session = FakeSession(results=[[]])
  _patch_session(monkeypatch, session)

  result = await SqlAlchemyAccountRepository().link_telegram(uuid.uuid4(), 555)
  assert result is None


async def test_unlink_telegram(monkeypatch):
  account = Account(
    account_id="acc-1", market_type=MarketTypeEnum.FOREX, telegram_user_id=555
  )
  session = FakeSession(results=[[account]])
  _patch_session(monkeypatch, session)

  ok = await SqlAlchemyAccountRepository().unlink_telegram(555)
  assert ok is True
  assert account.telegram_user_id is None


async def test_unlink_telegram_no_account(monkeypatch):
  session = FakeSession(results=[[]])
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
