from broker.domain.trade_status import TradeStatusPolicy
from broker.schemas.trade_schema import TradeStatusEnum


policy = TradeStatusPolicy()


def test_known_status_maps():
  assert policy.to_trade_status("OPENED") == TradeStatusEnum.OPENED
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
