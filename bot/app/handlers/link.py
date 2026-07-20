"""
app/handlers/link.py — FSM step: receive the UUID and link the account.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app import emojis
from app.presenters import messages
from app.services.broker_client import BrokerClientUser
from app.states import LinkAccount
from app.utils.invite import parse_code

router = Router(name="link")


async def start_link_flow(
  message: Message, state: FSMContext, *, already_linked: bool
) -> None:
  """Enter the "waiting for UUID token" FSM state, with wording that differs
  for a first-time linker vs. someone adding another account. Shared by
  ``start.py``'s onboarding path and ``/link`` below."""
  await state.set_state(LinkAccount.waiting_for_token)
  if already_linked:
    await message.answer(
      "Send the <b>UUID code</b> for the account you'd like to add.\n\n"
      "<i>Example: b5dc0374-9639-4861-acf4-2d239aa5c1b4</i>"
    )
  else:
    await message.answer(
      f"{emojis.WAVE} <b>Welcome!</b>\n\n"
      "Please send me the <b>UUID code</b> your admin gave you to link your "
      "account.\n\n"
      "<i>Example: b5dc0374-9639-4861-acf4-2d239aa5c1b4</i>"
    )


@router.message(Command("link"))
async def cmd_link(
  message: Message, state: FSMContext, broker: BrokerClientUser
) -> None:
  account = await broker.get_account(message.from_user.id)
  await start_link_flow(message, state, already_linked=account is not None)


async def apply_link_token(
  message: Message, state: FSMContext, broker: BrokerClientUser, raw: str
) -> bool:
  """Link the account behind code *raw* to the sender, answering with the
  outcome either way; returns whether it worked.

  Shared by the FSM step below (the user typed the code) and start.py's
  deep-link entry (the code came from an invite URL) — the latter falls back to
  prompting manually when this returns False."""
  token = parse_code(raw)
  if token is None:
    await message.answer(
      f"{emojis.WARNING} Invalid code. Please send the correct <b>UUID</b> your admin gave you."
    )
    return False

  account = await broker.link(token, message.from_user.id)
  if account is None:
    await message.answer(
      f"{emojis.CROSS} No account found with this code. Double-check it or contact your admin."
    )
    return False

  await state.clear()
  headline = (
    f"{emojis.CHECK} <b>Linked successfully!</b>"
    if account.get("is_active")
    else f"{emojis.CHECK} <b>Account added.</b> Still using another account as "
    "active — use /switch to change."
  )
  await message.answer(
    headline
    + "\n\n"
    + messages.UserMessages.format_account(account)
    + "\n\n"
    + messages.UserMessages.COMMANDS_HINT
  )
  return True


# Only treat non-command text as a candidate token while onboarding.
@router.message(LinkAccount.waiting_for_token, F.text & ~F.text.startswith("/"))
async def receive_token(
  message: Message, state: FSMContext, broker: BrokerClientUser
) -> None:
  await apply_link_token(message, state, broker, message.text or "")


# Non-text messages (photo, sticker…) while onboarding: prompt for UUID as text.
# Commands (text starting with "/") are excluded here so they fall through to
# their own handlers.
@router.message(LinkAccount.waiting_for_token, ~F.text)
async def prompt_text_token(message: Message) -> None:
  await message.answer(
    f"{emojis.WARNING} Please send the <b>UUID</b> code as text (not a photo/sticker)."
  )
