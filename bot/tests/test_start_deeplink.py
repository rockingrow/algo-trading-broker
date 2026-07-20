"""Tests for the /start deep-link entry — the invite URL's landing path."""

from __future__ import annotations

import pytest
from aiogram.filters import CommandStart
from aiogram.filters.command import CommandException, CommandObject

from app.handlers.link import apply_link_token
from app.handlers.start import cmd_start_deeplink
from app.states import LinkAccount

DASHED = "b5dc0374-9639-4861-acf4-2d239aa5c1b4"
HEX = "b5dc037496394861acf42d239aa5c1b4"


class FakeUser:
  id = 4242


class FakeMessage:
  def __init__(self):
    self.from_user = FakeUser()
    self.answers = []

  async def answer(self, text, **kwargs):
    self.answers.append(text)

  @property
  def text_of_all(self):
    return "\n".join(self.answers)


class FakeState:
  def __init__(self):
    self.state = None
    self.cleared = 0

  async def clear(self):
    self.cleared += 1
    self.state = None

  async def set_state(self, state):
    self.state = state


class FakeBroker:
  """``link`` returns an account for *known* tokens only; records every call."""

  def __init__(self, known=(DASHED,), account=None):
    self.known = set(known)
    self.account = account or {"account_id": "acc-1", "is_active": True}
    self.link_calls = []
    self.get_account_calls = []

  async def link(self, token, telegram_user_id):
    self.link_calls.append((token, telegram_user_id))
    return self.account if token in self.known else None

  async def get_account(self, telegram_user_id):
    self.get_account_calls.append(telegram_user_id)
    return None


# ── filter routing ──────────────────────────────────────────────────
# start.py registers CommandStart(deep_link=True) ahead of plain CommandStart()
# because the plain filter matches with or without a payload. These pin the
# behaviour that ordering depends on.


async def test_deep_link_filter_requires_a_payload():
  with pytest.raises(CommandException):
    # bot is only consulted for @mentions, which a bare "/start" has none of.
    await CommandStart(deep_link=True).parse_command("/start", bot=None)

  command = await CommandStart(deep_link=True).parse_command(f"/start {HEX}", bot=None)
  assert command.args == HEX


async def test_plain_start_filter_matches_with_payload_too():
  command = await CommandStart().parse_command(f"/start {HEX}", bot=None)
  assert command.args == HEX


# ── linking ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("payload", [DASHED, HEX])
async def test_deep_link_links_the_account_without_prompting(payload):
  message, state, broker = FakeMessage(), FakeState(), FakeBroker()

  await cmd_start_deeplink(
    message, CommandObject(command="start", args=payload), state, broker
  )

  # The broker always sees the canonical dashed form, whichever the URL carried.
  assert broker.link_calls == [(DASHED, 4242)]
  assert "Linked successfully" in message.text_of_all
  # No fallback prompt, and the FSM is not parked waiting for a token.
  assert "UUID" not in message.text_of_all
  assert state.state is None


async def test_deep_link_with_unknown_code_falls_back_to_manual_prompt():
  message, state, broker = FakeMessage(), FakeState(), FakeBroker(known=())

  await cmd_start_deeplink(
    message, CommandObject(command="start", args=DASHED), state, broker
  )

  assert broker.link_calls == [(DASHED, 4242)]
  assert "No account found" in message.text_of_all
  # Falls through to onboarding so the user can still type a code.
  assert state.state == LinkAccount.waiting_for_token


async def test_deep_link_with_a_malformed_payload_never_reaches_the_broker():
  message, state, broker = FakeMessage(), FakeState(), FakeBroker()

  await cmd_start_deeplink(
    message, CommandObject(command="start", args="junk"), state, broker
  )

  assert broker.link_calls == []
  assert "Invalid code" in message.text_of_all
  assert state.state == LinkAccount.waiting_for_token


async def test_apply_link_token_reports_a_non_active_add():
  message, state = FakeMessage(), FakeState()
  broker = FakeBroker(account={"account_id": "acc-2", "is_active": False})

  assert await apply_link_token(message, state, broker, DASHED) is True
  assert "Account added" in message.text_of_all
  assert state.cleared == 1
