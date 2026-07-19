"""
app/handlers/start.py — Onboarding entry: /start and /help.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app import emojis
from app.handlers.link import start_link_flow
from app.presenters import messages
from app.services.broker_client import BrokerClientUser

router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, broker: BrokerClientUser) -> None:
  account = await broker.get_account(message.from_user.id)
  if account is not None:
    await state.clear()
    await message.answer(
      f"{emojis.CHECK} Your account is already linked.\n\n"
      + messages.UserMessages.format_account(account)
      + "\n\n"
      + messages.UserMessages.COMMANDS_HINT
    )
    return

  await start_link_flow(message, state, already_linked=False)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
  await message.answer(messages.UserMessages.HELP_TEXT)
