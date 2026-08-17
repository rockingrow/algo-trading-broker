"""
broker/domain/broadcast_status.py — Lifecycle rules for a broadcast signal cycle.

Pure functions, no I/O: which actions end a cycle, and how a cycle's stored
status merges with the status implied by a newly arrived action. Kept out of
the repository and the service so both read the same rules and so the rules can
be unit-tested on their own.
"""

from __future__ import annotations

from broker.schemas.core import BroadcastStatusEnum, SignalActionEnum

#: Actions that end a cycle. TP1 is deliberately absent — a partial take-profit
#: leaves the rest of the position open, so the cycle keeps running.
CLOSING_ACTIONS: frozenset[SignalActionEnum] = frozenset(
  {
    SignalActionEnum.TP2,
    SignalActionEnum.SL,
    SignalActionEnum.R_SL,
    SignalActionEnum.FLAT,
  }
)


def status_for_action(action: SignalActionEnum) -> BroadcastStatusEnum:
  """Status a cycle takes when *action* is its most recent signal."""
  if action in CLOSING_ACTIONS:
    return BroadcastStatusEnum.CLOSED
  return BroadcastStatusEnum.RUNNING


def merge_status(
  current: BroadcastStatusEnum, incoming: BroadcastStatusEnum
) -> BroadcastStatusEnum:
  """Fold *incoming* into a cycle's *current* status.

  ``CLOSED`` is terminal: once a trade is out, a late or replayed signal (a
  TP1 arriving after the SL, a JetStream redelivery) must not advertise the
  position as live again.
  """
  if current == BroadcastStatusEnum.CLOSED:
    return BroadcastStatusEnum.CLOSED
  return incoming
