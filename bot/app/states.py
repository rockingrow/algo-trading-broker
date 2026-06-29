"""
app/states.py — FSM states for conversational flows.
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class LinkAccount(StatesGroup):
  """Onboarding flow: waiting for the user to send their account UUID."""

  waiting_for_token = State()
