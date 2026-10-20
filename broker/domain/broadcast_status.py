"""
broker/domain/broadcast_status.py — Lifecycle rules for a broadcast signal cycle.

Pure functions, no I/O: which actions end a cycle, which only take part of it
off, and how a cycle's stored status merges with the status implied by a newly
arrived action. Kept out of the repository and the service so both read the
same rules and so the rules can be unit-tested on their own.
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

#: Actions that take part of the position off without ending the cycle. They
#: carry a status of their own so the message header names the level reached
#: instead of still reading ``RUNNING``.
PARTIAL_ACTIONS: frozenset[SignalActionEnum] = frozenset({SignalActionEnum.TP1})

#: How far along its lifecycle each status sits. A cycle only ever moves
#: forward through this order, which is what :func:`merge_status` enforces.
_STATUS_ORDER: dict[BroadcastStatusEnum, int] = {
  BroadcastStatusEnum.RUNNING: 0,
  BroadcastStatusEnum.TP1: 1,
  BroadcastStatusEnum.CLOSED: 2,
}


def _as_status(value) -> BroadcastStatusEnum:
  """Coerce a stored status to an enum member.

  A row read back from Postgres hands over the member, but a fake, a partially
  loaded row or a value written by an older revision can be the bare string —
  and anything unrecognised is treated as a cycle that has not moved yet,
  because the merge below is what then carries it forward.
  """
  try:
    return BroadcastStatusEnum(getattr(value, "value", value))
  except ValueError:
    return BroadcastStatusEnum.RUNNING


def status_for_action(action: SignalActionEnum) -> BroadcastStatusEnum:
  """Status a cycle takes when *action* is its most recent signal."""
  if action in CLOSING_ACTIONS:
    return BroadcastStatusEnum.CLOSED
  if action in PARTIAL_ACTIONS:
    return BroadcastStatusEnum.TP1
  return BroadcastStatusEnum.RUNNING


def merge_status(
  current: BroadcastStatusEnum, incoming: BroadcastStatusEnum
) -> BroadcastStatusEnum:
  """Fold *incoming* into a cycle's *current* status.

  The lifecycle only runs forward. ``CLOSED`` is terminal: once a trade is
  out, a late or replayed signal (a TP1 arriving after the SL, a JetStream
  redelivery) must not advertise the position as live again. ``TP1`` is sticky
  for the same reason in the other direction: a scale-in entry arriving after
  the partial take-profit must not take the header back to ``RUNNING``, which
  would read as a trade that has given nothing back yet.
  """
  current_status = _as_status(current)
  incoming_status = _as_status(incoming)
  if _STATUS_ORDER[incoming_status] < _STATUS_ORDER[current_status]:
    return current_status
  return incoming_status
