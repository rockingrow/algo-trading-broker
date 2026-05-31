"""
broker/domain/trade_status.py — Business rules for how a worker's position
status maps onto the broker's trade lifecycle.

This is pure domain logic: no I/O, no SQLAlchemy. Keeping it here (rather than
inside the repository) means the persistence layer stays a thin data-access
wrapper, and these rules can be unit-tested in isolation.
"""

from __future__ import annotations

from broker.schemas.trade_schema import TradeStatusEnum


class TradeStatusPolicy:
  """Encapsulates the mapping and ordering rules between worker position
  statuses (OPENED/TP1/SL/…) and broker trade statuses."""

  # Map worker position status → broker trade status.
  _POSITION_STATUS_TO_TRADE_STATUS: dict[str, TradeStatusEnum] = {
    "OPENED": TradeStatusEnum.OPENED,
    "TP1": TradeStatusEnum.PARTIALLY_CLOSED,
    "TP2": TradeStatusEnum.CLOSED,
    "SL": TradeStatusEnum.CLOSED,
    "R_SL": TradeStatusEnum.CLOSED,
    "TERMINAL_CLOSED": TradeStatusEnum.CLOSED,
    "FORCED_CLOSED": TradeStatusEnum.CLOSED,
    "FLATTED": TradeStatusEnum.FLAT,
  }

  # Position statuses that represent a still-running trade.
  _OPEN_STATUSES = {"OPENED", "TP1"}

  # Monotonic lifecycle order; a transition to a lower rank is a downgrade.
  _STATUS_ORDER: dict[TradeStatusEnum, int] = {
    TradeStatusEnum.OPENED: 0,
    TradeStatusEnum.PARTIALLY_CLOSED: 1,
    TradeStatusEnum.CLOSED: 2,
    TradeStatusEnum.FLAT: 2,
    TradeStatusEnum.REJECTED: -1,
  }

  def to_trade_status(self, position_status: str) -> TradeStatusEnum | None:
    """Translate a worker position status into a broker trade status, or None
    if the status is unknown."""
    return self._POSITION_STATUS_TO_TRADE_STATUS.get(position_status)

  def is_open(self, position_status: str) -> bool:
    """Whether the given position status means the trade is still running."""
    return position_status in self._OPEN_STATUSES

  def is_downgrade(
    self, new_status: TradeStatusEnum, current_status: TradeStatusEnum
  ) -> bool:
    """Whether moving from *current_status* to *new_status* would regress the
    trade lifecycle (e.g. CLOSED → OPENED), which must be ignored."""
    return self._STATUS_ORDER.get(new_status, 0) < self._STATUS_ORDER.get(
      current_status, 0
    )
