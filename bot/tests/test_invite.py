"""Tests for invite code ↔ deep-link payload conversion."""

from __future__ import annotations

import pytest
from aiogram.utils.deep_linking import BAD_PATTERN, DEEPLINK_PAYLOAD_LENGTH

from app.utils.invite import parse_code, to_payload

DASHED = "b5dc0374-9639-4861-acf4-2d239aa5c1b4"
HEX = "b5dc037496394861acf42d239aa5c1b4"


@pytest.mark.parametrize("raw", [DASHED, HEX, f"  {DASHED}  ", DASHED.upper()])
def test_parse_code_accepts_either_form_and_canonicalises(raw):
  # The broker only ever sees the canonical dashed form, whatever was typed.
  assert parse_code(raw) == DASHED


@pytest.mark.parametrize("raw", ["", "   ", "not-a-uuid", "1234", None, 42])
def test_parse_code_rejects_non_uuids(raw):
  assert parse_code(raw) is None


def test_to_payload_is_bare_hex():
  assert to_payload(DASHED) == HEX
  assert to_payload(HEX) == HEX


def test_to_payload_rejects_non_uuids():
  assert to_payload("nope") is None
  assert to_payload("") is None


def test_payload_is_a_legal_telegram_deep_link_payload():
  """Telegram allows [A-Za-z0-9_-], max 64 chars — the same check aiogram's
  create_deep_link runs before building the URL."""
  payload = to_payload(DASHED)
  assert not BAD_PATTERN.search(payload)
  assert len(payload) <= DEEPLINK_PAYLOAD_LENGTH


def test_payload_round_trips_back_to_the_code():
  # What /start receives must resolve to the code the admin started from.
  assert parse_code(to_payload(DASHED)) == DASHED
