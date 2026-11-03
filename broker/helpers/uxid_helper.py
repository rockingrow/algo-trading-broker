"""
broker/helpers/uxid_helper.py — Short correlation ids for signal cycles.

A ``uxid`` is a 16-character uppercase-hex slice of a UUID4: short enough to
read out of a Telegram message or a log line, long enough (64 bits) that two
independent strategies never collide in practice. It is what ties every action
of one trade — the entry, its TP1/TP2, its SL, a FLAT — to the single broadcast
message that represents that cycle.

The shape is fixed on purpose: 16 characters, ``[0-9A-F]`` only. It is what
:func:`new_uxid` produces, and it is what the webhook validator accepts after
upper-casing what it was given — anything else is rejected at ingress, so a
malformed id can never make two unrelated trades look like the same cycle.
Hex, not alphanumeric: the id is a slice of a UUID4's hex, so a letter past
``F`` never occurs in a real one and accepting it would only widen what a
malformed alert can smuggle through.
"""

from __future__ import annotations

import re
import uuid

#: Characters kept from the UUID4 hex. 16 hex chars = 64 bits of randomness.
UXID_LENGTH = 16

#: The one shape a valid ``signal_uxid`` may take on the wire.
UXID_PATTERN = re.compile(rf"^[0-9A-F]{{{UXID_LENGTH}}}$")


def new_uxid(length: int = UXID_LENGTH) -> str:
  """Return a fresh short id, e.g. ``"9F2C4B7E18A3D605"``."""
  return uuid.uuid4().hex[:length].upper()


def is_valid_uxid(value: str) -> bool:
  """True when *value* matches the canonical uxid shape."""
  return bool(UXID_PATTERN.fullmatch(value))
