"""
broker/helpers/uxid_helper.py — Short correlation ids for signal cycles.

A ``uxid`` is a 16-character lowercase-hex slice of a UUID4: short enough to
read out of a Telegram message or a log line, long enough (64 bits) that two
independent strategies never collide in practice. It is what ties every action
of one trade — the entry, its TP1/TP2, its SL, a FLAT — to the single broadcast
message that represents that cycle.

The shape is fixed on purpose: 16 characters, ``[0-9a-f]`` only. It is what
:func:`new_uxid` produces, and it is what the webhook validator accepts —
anything else is rejected at ingress, so a malformed id can never make two
unrelated trades look like the same cycle.
"""

from __future__ import annotations

import re
import uuid

#: Characters kept from the UUID4 hex. 16 hex chars = 64 bits of randomness.
UXID_LENGTH = 16

#: The one shape a valid ``signal_uxid`` may take on the wire.
UXID_PATTERN = re.compile(rf"^[0-9a-f]{{{UXID_LENGTH}}}$")


def new_uxid(length: int = UXID_LENGTH) -> str:
  """Return a fresh short id, e.g. ``"9f2c4b7e18a3d605"``."""
  return uuid.uuid4().hex[:length]


def is_valid_uxid(value: str) -> bool:
  """True when *value* matches the canonical uxid shape."""
  return bool(UXID_PATTERN.fullmatch(value))
