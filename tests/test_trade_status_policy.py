from broker.domain.trade_status import TradeStatusPolicy
from broker.schemas.trade_schema import TradeStatusEnum


policy = TradeStatusPolicy()


def test_known_status_maps():
  assert policy.to_trade_status("OPENED") == TradeStatusEnum.OPENED
  assert policy.to_trade_status("REJECTED") == TradeStatusEnum.REJECTED
  assert policy.to_trade_status("TP1") == TradeStatusEnum.PARTIALLY_CLOSED
  assert policy.to_trade_status("TP2") == TradeStatusEnum.CLOSED
  assert policy.to_trade_status("FLATTED") == TradeStatusEnum.FLAT


def test_unknown_status_returns_none():
  assert policy.to_trade_status("NOPE") is None


def test_is_open():
  assert policy.is_open("OPENED") is True
  assert policy.is_open("TP1") is True
  assert policy.is_open("TP2") is False
  assert policy.is_open("SL") is False


def test_is_downgrade_blocks_regression():
  # CLOSED -> OPENED is a downgrade and must be flagged.
  assert policy.is_downgrade(TradeStatusEnum.OPENED, TradeStatusEnum.CLOSED) is True


def test_is_downgrade_allows_progression():
  assert policy.is_downgrade(TradeStatusEnum.CLOSED, TradeStatusEnum.OPENED) is False
  # Same rank (CLOSED == FLAT) is not a downgrade.
  assert policy.is_downgrade(TradeStatusEnum.FLAT, TradeStatusEnum.CLOSED) is False


def test_all_worker_statuses_map():
  expected = {
    "OPENED": TradeStatusEnum.OPENED,
    "REJECTED": TradeStatusEnum.REJECTED,
    "TP1": TradeStatusEnum.PARTIALLY_CLOSED,
    "TP2": TradeStatusEnum.CLOSED,
    "SL": TradeStatusEnum.CLOSED,
    "R_SL": TradeStatusEnum.CLOSED,
    "TERMINAL_CLOSED": TradeStatusEnum.CLOSED,
    "FORCED_CLOSED": TradeStatusEnum.CLOSED,
    "FLATTED": TradeStatusEnum.FLAT,
  }
  for worker_status, trade_status in expected.items():
    assert policy.to_trade_status(worker_status) == trade_status


def test_is_open_only_for_opened_and_tp1():
  assert policy.is_open("OPENED") is True
  assert policy.is_open("TP1") is True
  for closed in (
    "REJECTED",
    "TP2",
    "SL",
    "R_SL",
    "TERMINAL_CLOSED",
    "FORCED_CLOSED",
    "FLATTED",
  ):
    assert policy.is_open(closed) is False
  # Unknown statuses are not open.
  assert policy.is_open("WHATEVER") is False


def test_partially_closed_to_closed_is_progression():
  assert (
    policy.is_downgrade(TradeStatusEnum.CLOSED, TradeStatusEnum.PARTIALLY_CLOSED)
    is False
  )


def test_closed_to_partially_closed_is_downgrade():
  assert (
    policy.is_downgrade(TradeStatusEnum.PARTIALLY_CLOSED, TradeStatusEnum.CLOSED)
    is True
  )


def test_rejected_is_lowest_rank():
  # REJECTED ranks below OPENED: moving to REJECTED from a live status is a
  # downgrade, while leaving REJECTED for a live status is progression.
  assert policy.is_downgrade(TradeStatusEnum.REJECTED, TradeStatusEnum.OPENED) is True
  assert policy.is_downgrade(TradeStatusEnum.OPENED, TradeStatusEnum.REJECTED) is False
