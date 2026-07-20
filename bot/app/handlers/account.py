"""
app/handlers/account.py — /status and /unlink (protected router).

Callback-data scheme:
- myacc:{offset}   /myaccounts pagination
- swpg:{offset}    /switch pagination (table + picker move together)
- swacc:{uuid}     /switch picker → make that account active
- unlink:confirm|cancel
"""

from __future__ import annotations

from typing import Any, Optional

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app import emojis
from app.constants import ACCOUNTS_PER_PAGE
from app.presenters import messages
from app.utils.pagination import paginate
from app.utils.telegram import safe_edit_text
from app.keyboards import inline
from app.services.broker_client import BrokerClientUser

router = Router(name="account")


def _offset_from(data: str) -> Optional[int]:
  """Parse the ``{prefix}:{offset}`` tail of callback data, or None if it isn't
  a number — a malformed callback is ignored rather than raising."""
  raw = data.split(":", 1)[1] if ":" in data else ""
  return int(raw) if raw.isdigit() else None


@router.message(Command("status"))
async def cmd_status(message: Message, account: dict[str, Any]) -> None:
  await message.answer(messages.UserMessages.format_account(account))


# ── /myaccounts — list all accounts linked to this Telegram user ──────
# The broker returns every linked account in one response, so the page is
# sliced here rather than requested — see utils.pagination.paginate.


async def _myaccounts_view(
  broker: BrokerClientUser, telegram_user_id: int, offset: int
) -> tuple[Optional[str], Optional[InlineKeyboardMarkup]]:
  accounts = await broker.list_accounts(telegram_user_id)
  if accounts is None:
    return None, None
  rows, page = paginate(accounts, ACCOUNTS_PER_PAGE, offset)
  return (
    messages.UserMessages.format_accounts_list(rows, page, with_switch_hint=False),
    inline.accounts_pagination(page),
  )


@router.message(Command("myaccounts"))
async def cmd_myaccounts(message: Message, broker: BrokerClientUser) -> None:
  text, kb = await _myaccounts_view(broker, message.from_user.id, 0)
  if text is None:
    await message.answer(f"{emojis.WARNING} Failed to fetch your accounts. Try again later.")
    return
  await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("myacc:"))
async def cb_myaccounts_page(call: CallbackQuery, broker: BrokerClientUser) -> None:
  offset = _offset_from(call.data)
  if offset is None:
    await call.answer()
    return
  text, kb = await _myaccounts_view(broker, call.from_user.id, offset)
  if text is None:
    await call.answer("Failed to load data", show_alert=True)
    return
  await safe_edit_text(call.message, text, kb)
  await call.answer()


# ── /switch — change which linked account is active ───────────────────
# AuthMiddleware already resolved an active account before this handler runs,
# so the caller is guaranteed to have at least one linked account.


async def _switch_view(
  broker: BrokerClientUser, telegram_user_id: int, offset: int
) -> tuple[Optional[str], Optional[InlineKeyboardMarkup]]:
  """The table and the picker are built from the same page slice, so a button
  always sits under the row it belongs to."""
  accounts = await broker.list_accounts(telegram_user_id)
  if accounts is None:
    return None, None
  rows, page = paginate(accounts, ACCOUNTS_PER_PAGE, offset)
  return (
    messages.UserMessages.format_accounts_list(rows, page),
    inline.linked_accounts_picker(rows, page),
  )


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
  text, kb = await _switch_view(broker, message.from_user.id, 0)
  if text is None:
    await message.answer(f"{emojis.WARNING} Failed to fetch your accounts. Try again later.")
    return
  await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("swpg:"))
async def cb_switch_page(call: CallbackQuery, broker: BrokerClientUser) -> None:
  offset = _offset_from(call.data)
  if offset is None:
    await call.answer()
    return
  text, kb = await _switch_view(broker, call.from_user.id, offset)
  if text is None:
    await call.answer("Failed to load data", show_alert=True)
    return
  await safe_edit_text(call.message, text, kb)
  await call.answer()


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
