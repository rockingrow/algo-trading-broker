"""
app/handlers/start.py — Onboarding entry: /start and /help.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app import emojis
from app.handlers.link import apply_link_token, start_link_flow
from app.presenters import messages
from app.services.broker_client import BrokerClientUser

router = Router(name="start")


# ``/start <code>`` — the invite-link entry (see /admin_invite_url). Tapping a
# t.me invite URL opens the bot with the account's link token already in the
# payload, so the account is linked on the spot instead of the user being asked
# to paste a UUID. Registered before the bare /start below because plain
# ``CommandStart()`` matches with or without a payload, and aiogram tries
# handlers in registration order.
@router.message(CommandStart(deep_link=True))
async def cmd_start_deeplink(
  message: Message, command: CommandObject, state: FSMContext, broker: BrokerClientUser
) -> None:
  await state.clear()
  if await apply_link_token(message, state, broker, command.args or ""):
    return

  # Bad or unknown code: apply_link_token already said why, so just drop into
  # the normal onboarding prompt.
  account = await broker.get_account(message.from_user.id)
  await start_link_flow(message, state, already_linked=account is not None)


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
