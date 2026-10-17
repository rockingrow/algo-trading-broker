"""
app/handlers/commands.py — Trading control: /flat, /prevent, /allow, /maxorders.

The first three show a confirmation keyboard; the actual broker call happens only
after the user taps "Confirm". /maxorders is a short FSM flow instead — it needs
a number, not a yes/no, and it places no orders of its own, so showing the
current cap and reading a reply is the whole interaction. Protected router
(requires a linked account).
"""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import emojis
from app.presenters import messages
from app.presenters.messages import UserMessages
from app.states import SetMaxOpenOrders
from app.utils.telegram import safe_edit_text
from app.keyboards import inline
from app.services.broker_client import BrokerClientUser

router = Router(name="commands")


@router.message(Command("flat"))
async def cmd_flat(message: Message, account: dict[str, Any]) -> None:
  await message.answer(
    f"{emojis.WARNING} Confirm <b>FLAT</b> (close all positions) for account "
    f"<code>{account.get('account_id')}</code>?",
    reply_markup=inline.confirm_keyboard("flat"),
  )


@router.message(Command("prevent"))
async def cmd_prevent(message: Message, account: dict[str, Any]) -> None:
  await message.answer(
    f"{emojis.WARNING} Confirm <b>BLOCK</b> new orders for account "
    f"<code>{account.get('account_id')}</code>?",
    reply_markup=inline.confirm_keyboard("prevent"),
  )


@router.message(Command("allow"))
async def cmd_allow(message: Message, account: dict[str, Any]) -> None:
  await message.answer(
    f"Confirm <b>ALLOW</b> new orders for account "
    f"<code>{account.get('account_id')}</code>?",
    reply_markup=inline.confirm_keyboard("allow"),
  )


# ── /maxorders ──────────────────────────────────────────────────────
# Show the cap in force for the active account and read a new one. The bot
# validates locally so an obvious typo costs no broker round-trip; the broker's
# own ge=0 check stays the source of truth. "-" clears the user's own value and
# hands the account back to the broker-wide default, which is a different
# intent from "no limit" (0) — see the broker's MaxOpenOrdersRequest.


@router.message(Command("maxorders"))
async def cmd_max_orders(
  message: Message, state: FSMContext, broker: BrokerClientUser
) -> None:
  await state.clear()
  current = await broker.get_max_open_orders(message.from_user.id)
  if current is None:
    await message.answer(f"{emojis.WARNING} Failed to fetch the current limit.")
    return
  await state.set_state(SetMaxOpenOrders.waiting_for_value)
  await message.answer(UserMessages.format_max_open_orders(current))


@router.message(SetMaxOpenOrders.waiting_for_value, Command("cancel"))
async def cancel_max_orders(message: Message, state: FSMContext) -> None:
  await state.clear()
  await message.answer("Cancelled.")


@router.message(SetMaxOpenOrders.waiting_for_value, F.text & ~F.text.startswith("/"))
async def receive_max_orders(
  message: Message, state: FSMContext, broker: BrokerClientUser
) -> None:
  raw = (message.text or "").strip()
  if raw == "-":
    value: int | None = None
  else:
    try:
      value = int(raw)
    except ValueError:
      await message.answer(
        f"{emojis.WARNING} Please send a whole number (e.g. <code>5</code>), "
        "<code>0</code> for no limit, or <code>-</code> for the default. "
        "Send /cancel to abort."
      )
      return
    if value < 0:
      await message.answer(
        f"{emojis.WARNING} The limit cannot be negative. Send <code>0</code> for "
        "no limit, or <code>-</code> for the default. Send /cancel to abort."
      )
      return

  result = await broker.set_max_open_orders(message.from_user.id, value)
  await state.clear()
  if result is None:
    await message.answer(
      f"{emojis.CROSS} Failed to update the limit. Run /maxorders to retry."
    )
    return
  await message.answer(UserMessages.format_max_open_orders_updated(result))


@router.message(SetMaxOpenOrders.waiting_for_value, ~F.text)
async def prompt_max_orders_text(message: Message) -> None:
  await message.answer(
    f"{emojis.WARNING} Please send the limit as a number, or <code>-</code> for "
    "the default."
  )


@router.callback_query(F.data.in_({"flat:confirm", "prevent:confirm", "allow:confirm"}))
async def cb_confirm(call: CallbackQuery, broker: BrokerClientUser) -> None:
  action = call.data.split(":", 1)[0]
  tg_id = call.from_user.id

  if action == "flat":
    result = await broker.flat(tg_id)
  elif action == "prevent":
    result = await broker.prevent(tg_id, enabled=True)
  else:  # allow
    result = await broker.prevent(tg_id, enabled=False)

  if result is None:
    await safe_edit_text(
      call.message, f"{emojis.CROSS} Command failed. Try again later."
    )
  else:
    await safe_edit_text(call.message, messages.format_command_result(result))
  await call.answer()


@router.callback_query(F.data.in_({"flat:cancel", "prevent:cancel", "allow:cancel"}))
async def cb_cancel(call: CallbackQuery) -> None:
  await safe_edit_text(call.message, "Cancelled.")
  await call.answer()
