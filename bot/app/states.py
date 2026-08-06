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


class SetStrategyMagicMap(StatesGroup):
  """Admin flow: type the strategy → magic-number map as a JSON object."""

  waiting_for_value = State()


class AdminCryptoAllowedSymbol(StatesGroup):
  """Admin flow: after seeing the current allowed-symbol list, type a new one
  as a comma-separated list of symbols (e.g. ``BTC, ETH, SOL``)."""

  waiting_for_symbols = State()


class AdminCryptoMaxLeverage(StatesGroup):
  """Admin flow: after seeing the current default leverage, type a new positive
  integer."""

  waiting_for_leverage = State()
