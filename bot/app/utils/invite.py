"""
app/utils/invite.py — Invite code ↔ /start deep-link payload.

The invite code *is* the account's link token (the UUID an end user would
otherwise paste at /start), so a deep link just carries that token as the
payload: ``https://t.me/<bot>?start=<payload>``.

Telegram's payload alphabet is ``[A-Za-z0-9_-]`` (max 64 chars), which the
dashed UUID form already satisfies — but nobody reads this payload, they tap
it, so it travels as bare hex (32 chars instead of 36) to keep the shared URL
short. ``parse_code`` accepts either form, so a code typed by hand still works.
"""

from __future__ import annotations

import uuid
from typing import Optional


def parse_code(raw: str) -> Optional[str]:
  """Canonical dashed UUID for *raw* (dashed or bare hex), or None if *raw*
  isn't a UUID at all. The broker is always given this canonical form."""
  try:
    return str(uuid.UUID(raw.strip()))
  except (AttributeError, TypeError, ValueError):
    return None


def to_payload(code: str) -> Optional[str]:
  """Deep-link payload (bare hex) for *code*, or None if it isn't a UUID."""
  try:
    return uuid.UUID(code.strip()).hex
  except (AttributeError, TypeError, ValueError):
    return None
