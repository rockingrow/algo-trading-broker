"""
app/handlers/admin.py — Admin commands (protected by IsAdmin at router level).

Admins do NOT need a linked account, so this router carries no AuthMiddleware —
only the IsAdmin filter. Everything maps to existing broker management endpoints.

Callback-data scheme (all well under Telegram's 64-byte limit; account_id is
String(50) and never contains ':'):
- atrp:{account_id}            picker → show trades
- atr:{account_id}:{offset}    trades pagination
- aflat:{target}:ok|no         target '*' = flat everything
- arotp:{account_id}           picker → rotate confirm
- arot:{account_id}:ok|no      rotate confirm
- aset:{slug}                  toggle a broker setting
"""

from __future__ import annotations

import html
from typing import Optional

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app import emojis
from app.config import settings
from app.filters.admin import IsAdmin
from app.presenters import messages
from app.utils.telegram import safe_edit_text
from app.keyboards import inline
from app.services.broker_client import BrokerClient

router = Router(name="admin")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

PAGE_SIZE = settings.BOT_VIEW_TRADES_PER_PAGE


# ── /accounts ───────────────────────────────────────────────────────


@router.message(Command("accounts"))
async def cmd_accounts(message: Message, broker: BrokerClient) -> None:
  accounts = await broker.admin_list_accounts()
  if accounts is None:
    await message.answer(f"{emojis.WARNING} Failed to fetch account list.")
    return
  await message.answer(messages.AdminMessages.format_accounts_admin(accounts))


# ── /atrades ────────────────────────────────────────────────────────


async def _atrades_view(
  broker: BrokerClient, account_id: str, offset: int
) -> tuple[Optional[str], Optional[InlineKeyboardMarkup]]:
  payload = await broker.admin_list_trades(account_id, limit=PAGE_SIZE, offset=offset)
  if payload is None:
    return None, None
  return (
    messages.AdminMessages.format_admin_trades(account_id, payload),
    inline.admin_trades_pagination(account_id, payload.get("page", {})),
  )


@router.message(Command("atrades"))
async def cmd_atrades(
  message: Message, command: CommandObject, broker: BrokerClient
) -> None:
  arg = (command.args or "").strip()
  if arg:
    text, kb = await _atrades_view(broker, arg, 0)
    if text is None:
      await message.answer(f"{emojis.WARNING} Failed to fetch trades for this account.")
      return
    await message.answer(text, reply_markup=kb)
    return

  accounts = await broker.admin_list_accounts()
  if not accounts:
    await message.answer("No accounts.")
    return
  await message.answer(
    "Choose an account to view trades:",
    reply_markup=inline.accounts_picker(accounts, "atrp"),
  )


@router.callback_query(F.data.startswith("atrp:"))
async def cb_atrades_pick(call: CallbackQuery, broker: BrokerClient) -> None:
  account_id = call.data.split(":", 1)[1]
  text, kb = await _atrades_view(broker, account_id, 0)
  if text is None:
    await call.answer("Failed to load data", show_alert=True)
    return
  await safe_edit_text(call.message, text, kb)
  await call.answer()


@router.callback_query(F.data.startswith("atr:"))
async def cb_atrades_page(call: CallbackQuery, broker: BrokerClient) -> None:
  parts = call.data.split(":")  # ["atr", account_id, offset]
  if len(parts) != 3 or not parts[2].isdigit():
    await call.answer()
    return
  text, kb = await _atrades_view(broker, parts[1], int(parts[2]))
  if text is None:
    await call.answer("Failed to load data", show_alert=True)
    return
  await safe_edit_text(call.message, text, kb)
  await call.answer()


# ── /aflat ──────────────────────────────────────────────────────────


@router.message(Command("aflat"))
async def cmd_aflat(message: Message, command: CommandObject) -> None:
  arg = (command.args or "").strip()
  if arg:
    scope_txt, target = f"account <code>{html.escape(arg)}</code>", arg
  else:
    scope_txt, target = "<b>ALL</b> accounts", "*"
  await message.answer(
    f"{emojis.WARNING} Confirm <b>FLAT</b> (close positions) for {scope_txt}?",
    reply_markup=inline.admin_confirm("aflat", target),
  )


@router.callback_query(F.data.startswith("aflat:"))
async def cb_aflat(call: CallbackQuery, broker: BrokerClient) -> None:
  parts = call.data.split(":")  # ["aflat", target, decision]
  if len(parts) != 3:
    await call.answer()
    return
  target, decision = parts[1], parts[2]
  if decision != "ok":
    await safe_edit_text(call.message, "Cancelled.")
    await call.answer()
    return
  result = await broker.admin_flat(account_id=None if target == "*" else target)
  if result is None:
    await safe_edit_text(call.message, f"{emojis.CROSS} FLAT failed.")
  else:
    await safe_edit_text(call.message, messages.format_command_result(result))
  await call.answer()


# ── /rotate ─────────────────────────────────────────────────────────


def _rotate_prompt(account_id: str) -> str:
  return (
    f"{emojis.WARNING} Rotate link token for <code>{html.escape(account_id)}</code>? "
    "The old token will be revoked."
  )


@router.message(Command("rotate"))
async def cmd_rotate(
  message: Message, command: CommandObject, broker: BrokerClient
) -> None:
  arg = (command.args or "").strip()
  if arg:
    await message.answer(
      _rotate_prompt(arg), reply_markup=inline.admin_confirm("arot", arg)
    )
    return
  accounts = await broker.admin_list_accounts()
  if not accounts:
    await message.answer("No accounts.")
    return
  await message.answer(
    "Choose an account to rotate its token:",
    reply_markup=inline.accounts_picker(accounts, "arotp"),
  )


@router.callback_query(F.data.startswith("arotp:"))
async def cb_rotate_pick(call: CallbackQuery) -> None:
  account_id = call.data.split(":", 1)[1]
  await safe_edit_text(
    call.message, _rotate_prompt(account_id), inline.admin_confirm("arot", account_id)
  )
  await call.answer()


@router.callback_query(F.data.startswith("arot:"))
async def cb_rotate(call: CallbackQuery, broker: BrokerClient) -> None:
  parts = call.data.split(":")  # ["arot", account_id, decision]
  if len(parts) != 3:
    await call.answer()
    return
  account_id, decision = parts[1], parts[2]
  if decision != "ok":
    await safe_edit_text(call.message, "Cancelled.")
    await call.answer()
    return
  result = await broker.admin_rotate_token(account_id)
  if result is None:
    await safe_edit_text(call.message, f"{emojis.CROSS} Token rotation failed.")
  else:
    await safe_edit_text(call.message, messages.AdminMessages.format_rotate_result(result))
  await call.answer()


# ── /settings ───────────────────────────────────────────────────────


async def _render_settings(
  broker: BrokerClient,
) -> tuple[Optional[str], Optional[InlineKeyboardMarkup]]:
  states = await broker.admin_get_settings()
  if states is None:
    return None, None
  return messages.AdminMessages.format_settings(states), inline.settings_keyboard(states)


@router.message(Command("settings"))
async def cmd_settings(message: Message, broker: BrokerClient) -> None:
  text, kb = await _render_settings(broker)
  if text is None:
    await message.answer(f"{emojis.WARNING} Failed to fetch broker settings.")
    return
  await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("aset:"))
async def cb_settings_toggle(call: CallbackQuery, broker: BrokerClient) -> None:
  slug = call.data.split(":", 1)[1]
  await broker.admin_toggle_setting(slug)
  text, kb = await _render_settings(broker)
  if text is None:
    await call.answer("Error", show_alert=True)
    return
  await safe_edit_text(call.message, text, kb)
  await call.answer("Updated")
