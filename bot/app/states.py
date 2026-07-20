"""
app/states.py — FSM states for conversational flows.
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class LinkAccount(StatesGroup):
  """Onboarding flow: waiting for the user to send their account UUID."""

  waiting_for_token = State()


class CreateAccount(StatesGroup):
  """Admin flow: market + gateway picked via inline keyboard, then the
  account_id suffix is typed as free text."""

  waiting_for_account_id = State()


class AdminLinkAccount(StatesGroup):
  """Admin flow: pick an account via inline keyboard, then type the Telegram
  user id to bind to it."""

  waiting_for_telegram_id = State()
