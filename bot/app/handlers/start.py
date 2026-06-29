"""
app/handlers/start.py — Onboarding entry: /start and /help.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.formatters import messages
from app.services.broker_client import BrokerClient
from app.states import LinkAccount

router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, broker: BrokerClient) -> None:
  account = await broker.get_account(message.from_user.id)
  if account is not None:
    await state.clear()
    await message.answer(
      "✅ Tài khoản của bạn đã được liên kết.\n\n"
      + messages.format_account(account)
      + "\n\n"
      + messages.COMMANDS_HINT
    )
    return

  await state.set_state(LinkAccount.waiting_for_token)
  await message.answer(
    "👋 <b>Chào mừng!</b>\n\n"
    "Hãy gửi cho tôi <b>mã UUID</b> mà quản trị viên đã cấp để liên kết tài khoản "
    "của bạn.\n\n"
    "<i>Ví dụ: b5dc0374-9639-4861-acf4-2d239aa5c1b4</i>"
  )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
  await message.answer(messages.HELP_TEXT)
