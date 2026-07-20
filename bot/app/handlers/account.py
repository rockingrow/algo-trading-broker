"""
app/handlers/account.py — /status and /unlink (protected router).
"""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app import emojis
from app.presenters import messages
from app.utils.telegram import safe_edit_text
from app.keyboards import inline
from app.services.broker_client import BrokerClientUser

router = Router(name="account")


@router.message(Command("status"))
async def cmd_status(message: Message, account: dict[str, Any]) -> None:
  await message.answer(messages.UserMessages.format_account(account))


# ── /myaccounts — list all accounts linked to this Telegram user ──────


@router.message(Command("myaccounts"))
async def cmd_myaccounts(message: Message, broker: BrokerClientUser) -> None:
  accounts = await broker.list_accounts(message.from_user.id)
  if accounts is None:
    await message.answer(f"{emojis.WARNING} Failed to fetch your accounts. Try again later.")
    return
  await message.answer(messages.UserMessages.format_accounts_list(accounts, with_switch_hint=False))


# ── /switch — change which linked account is active ───────────────────
# AuthMiddleware already resolved an active account before this handler runs,
# so the caller is guaranteed to have at least one linked account.


@router.message(Command("switch"))
async def cmd_switch(message: Message, broker: BrokerClientUser) -> None:
  accounts = await broker.list_accounts(message.from_user.id)
  if accounts is None:
    await message.answer(f"{emojis.WARNING} Failed to fetch your accounts. Try again later.")
    return
  if len(accounts) <= 1:
    await message.answer(
      "You only have one linked account — nothing to switch to. Use /link to add another."
    )
    return
  await message.answer(
    messages.UserMessages.format_accounts_list(accounts),
    reply_markup=inline.linked_accounts_picker(accounts),
  )


@router.callback_query(F.data.startswith("swacc:"))
async def cb_switch_account(call: CallbackQuery, broker: BrokerClientUser) -> None:
  account_id = call.data.split(":", 1)[1]
  result = await broker.switch_account(call.from_user.id, account_id)
  if result is None:
    await safe_edit_text(call.message, f"{emojis.CROSS} Switch failed. Try again later.")
    await call.answer()
    return
  await safe_edit_text(
    call.message,
    f"{emojis.CHECK} Active account switched.\n\n" + messages.UserMessages.format_account(result),
  )
  await call.answer()


# ── /subscribe, /unsubscribe — completed-trade broadcast opt-in ───────
# A per-user preference (spans every linked account), toggled here. Lives on
# the protected router so only linked owners can opt in — an unlinked user has
# no account whose trades could complete.


@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message, broker: BrokerClientUser) -> None:
  result = await broker.subscribe_broadcast(message.from_user.id)
  if result is None:
    await message.answer(
      f"{emojis.WARNING} Failed to update subscription. Try again later."
    )
    return
  await message.answer(messages.UserMessages.format_broadcast_subscription(True))


@router.message(Command("unsubscribe"))
async def cmd_unsubscribe(message: Message, broker: BrokerClientUser) -> None:
  result = await broker.unsubscribe_broadcast(message.from_user.id)
  if result is None:
    await message.answer(
      f"{emojis.WARNING} Failed to update subscription. Try again later."
    )
    return
  await message.answer(messages.UserMessages.format_broadcast_subscription(False))


@router.message(Command("unlink"))
async def cmd_unlink(message: Message, account: dict[str, Any]) -> None:
  await message.answer(
    "Are you sure you want to <b>unlink</b> your active account "
    f"(<code>{account.get('account_id')}</code>) from Telegram?",
    reply_markup=inline.confirm_keyboard("unlink"),
  )


@router.callback_query(F.data == "unlink:confirm")
async def cb_unlink(call: CallbackQuery, broker: BrokerClientUser) -> None:
  ok = await broker.unlink(call.from_user.id)
  await safe_edit_text(
    call.message,
    f"{emojis.CHECK} Unlinked. Type /start to link again."
    if ok
    else f"{emojis.CROSS} Unlink failed. Try again later.",
  )
  await call.answer()


@router.callback_query(F.data == "unlink:cancel")
async def cb_unlink_cancel(call: CallbackQuery) -> None:
  await safe_edit_text(call.message, "Cancelled.")
  await call.answer()
