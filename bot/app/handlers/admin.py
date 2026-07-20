"""
app/handlers/admin.py — Admin commands (protected by IsAdmin at router level).

Admins do NOT need a linked account, so this router carries no AuthMiddleware —
only the IsAdmin filter. Everything maps to existing broker management endpoints.

Callback-data scheme (all well under Telegram's 64-byte limit; account_id is
String(50) and never contains ':'):
- atrp:{account_id}            picker → show trades
- atr:{account_id}:{offset}    trades pagination
- aflat:confirm|cancel         target resolved server-side, kept in FSM data
                                (account_id alone can't go in callback_data —
                                the broker now requires market + gateway
                                alongside it, see admin_flat's docstring)
- aflatc:{index}               disambiguation picker → picks aflat_candidates[index]
- arotp:{account_id}           picker → rotate confirm
- arot:{account_id}:ok|no      rotate confirm
- aset:{slug}                  toggle a broker setting
- nacc:m:{market}               /newaccount → gateway picker
- nacc:g:{market}:{gateway}     gateway picker → prompt for account_id
"""

from __future__ import annotations

import html
from typing import Optional

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app import emojis
from app.config import settings
from app.constants import GATEWAYS_BY_MARKET, MARKETS
from app.filters.is_admin import IsAdmin
from app.presenters import messages
from app.states import AdminLinkAccount, CreateAccount
from app.utils.telegram import safe_edit_text
from app.utils.timezone import offset_hours_from_payload
from app.keyboards import inline
from app.services.broker_client import BrokerClientAdmin

router = Router(name="admin")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

PAGE_SIZE = settings.BOT_VIEW_TRADES_PER_PAGE


# ── /accounts ───────────────────────────────────────────────────────


@router.message(Command("admin_accounts", "accounts"))
async def cmd_accounts(message: Message, broker_admin: BrokerClientAdmin) -> None:
  accounts = await broker_admin.admin_list_accounts()
  if accounts is None:
    await message.answer(f"{emojis.WARNING} Failed to fetch account list.")
    return
  await message.answer(messages.AdminMessages.format_accounts_admin(accounts))


# ── /atrades ────────────────────────────────────────────────────────


async def _atrades_view(
  broker_admin: BrokerClientAdmin, account_id: str, offset: int
) -> tuple[Optional[str], Optional[InlineKeyboardMarkup]]:
  payload = await broker_admin.admin_list_trades(account_id, limit=PAGE_SIZE, offset=offset)
  if payload is None:
    return None, None
  tz_offset = offset_hours_from_payload(await broker_admin.get_notification_timezone())
  return (
    messages.AdminMessages.format_admin_trades(account_id, payload, tz_offset),
    inline.admin_trades_pagination(account_id, payload.get("page", {})),
  )


@router.message(Command("admin_trades", "atrades"))
async def cmd_atrades(
  message: Message, command: CommandObject, broker_admin: BrokerClientAdmin
) -> None:
  arg = (command.args or "").strip()
  if arg:
    text, kb = await _atrades_view(broker_admin, arg, 0)
    if text is None:
      await message.answer(f"{emojis.WARNING} Failed to fetch trades for this account.")
      return
    await message.answer(text, reply_markup=kb)
    return

  accounts = await broker_admin.admin_list_accounts()
  if not accounts:
    await message.answer("No accounts.")
    return
  await message.answer(
    "Choose an account to view trades:",
    reply_markup=inline.accounts_picker(accounts, "atrp"),
  )


@router.callback_query(F.data.startswith("atrp:"))
async def cb_atrades_pick(call: CallbackQuery, broker_admin: BrokerClientAdmin) -> None:
  account_id = call.data.split(":", 1)[1]
  text, kb = await _atrades_view(broker_admin, account_id, 0)
  if text is None:
    await call.answer("Failed to load data", show_alert=True)
    return
  await safe_edit_text(call.message, text, kb)
  await call.answer()


@router.callback_query(F.data.startswith("atr:"))
async def cb_atrades_page(call: CallbackQuery, broker_admin: BrokerClientAdmin) -> None:
  parts = call.data.split(":")  # ["atr", account_id, offset]
  if len(parts) != 3 or not parts[2].isdigit():
    await call.answer()
    return
  text, kb = await _atrades_view(broker_admin, parts[1], int(parts[2]))
  if text is None:
    await call.answer("Failed to load data", show_alert=True)
    return
  await safe_edit_text(call.message, text, kb)
  await call.answer()


# ── /aflat ──────────────────────────────────────────────────────────
# account_id alone no longer identifies a single account (the broker now
# requires market + gateway alongside it — see FlatRequest's docstring),
# so scoping to one account resolves those from the live account list first.
# Because that resolved target can't safely fit in callback_data (well under
# 64 bytes for a worst-case 50-char account_id + market/gateway), it's kept
# in FSM data instead; only the confirm/cancel decision travels on the wire.


def _aflat_confirm_text(account: dict) -> str:
  return (
    f"{emojis.WARNING} Confirm <b>FLAT</b> (close positions) for account "
    f"<code>{html.escape(str(account.get('account_id')))}</code> "
    f"({html.escape(str(account.get('market')))}/"
    f"{html.escape(str(account.get('gateway')))})?"
  )


@router.message(Command("admin_flat", "aflat"))
async def cmd_aflat(
  message: Message,
  command: CommandObject,
  state: FSMContext,
  broker_admin: BrokerClientAdmin,
) -> None:
  arg = (command.args or "").strip()
  if not arg:
    await state.update_data(aflat_target="*", aflat_candidates=None)
    await message.answer(
      f"{emojis.WARNING} Confirm <b>FLAT</b> (close positions) for <b>ALL</b> accounts?",
      reply_markup=inline.confirm_keyboard("aflat"),
    )
    return

  accounts = await broker_admin.admin_list_accounts() or []
  matches = [a for a in accounts if a.get("account_id") == arg]

  if not matches:
    await message.answer(
      f"{emojis.WARNING} No account found with id <code>{html.escape(arg)}</code>."
    )
    return

  if len(matches) > 1:
    await state.update_data(aflat_target=None, aflat_candidates=matches)
    await message.answer(
      f"{emojis.WARNING} <code>{html.escape(arg)}</code> matches {len(matches)} accounts "
      "on different gateways — pick the one to flat:",
      reply_markup=inline.aflat_candidates_picker(matches),
    )
    return

  account = matches[0]
  await state.update_data(aflat_target=account, aflat_candidates=None)
  await message.answer(_aflat_confirm_text(account), reply_markup=inline.confirm_keyboard("aflat"))


@router.callback_query(F.data.startswith("aflatc:"))
async def cb_aflat_pick(call: CallbackQuery, state: FSMContext) -> None:
  try:
    idx = int(call.data.split(":", 1)[1])
  except (IndexError, ValueError):
    await call.answer()
    return
  data = await state.get_data()
  candidates = data.get("aflat_candidates") or []
  if idx < 0 or idx >= len(candidates):
    await call.answer(f"{emojis.WARNING} Expired — run /aflat again.", show_alert=True)
    return

  account = candidates[idx]
  await state.update_data(aflat_target=account, aflat_candidates=None)
  await safe_edit_text(call.message, _aflat_confirm_text(account), inline.confirm_keyboard("aflat"))
  await call.answer()


@router.callback_query(F.data.in_({"aflat:confirm", "aflat:cancel"}))
async def cb_aflat(call: CallbackQuery, state: FSMContext, broker_admin: BrokerClientAdmin) -> None:
  decision = call.data.split(":", 1)[1]
  data = await state.get_data()
  target = data.get("aflat_target")
  await state.update_data(aflat_target=None, aflat_candidates=None)

  if decision != "confirm" or target is None:
    await safe_edit_text(call.message, "Cancelled.")
    await call.answer()
    return

  if target == "*":
    result = await broker_admin.admin_flat()
  else:
    result = await broker_admin.admin_flat(
      account_id=target.get("account_id"),
      market=target.get("market"),
      gateway=target.get("gateway"),
    )

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


@router.message(Command("admin_rotate", "rotate"))
async def cmd_rotate(
  message: Message, command: CommandObject, broker_admin: BrokerClientAdmin
) -> None:
  arg = (command.args or "").strip()
  if arg:
    await message.answer(
      _rotate_prompt(arg), reply_markup=inline.admin_confirm("arot", arg)
    )
    return
  accounts = await broker_admin.admin_list_accounts()
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
async def cb_rotate(call: CallbackQuery, broker_admin: BrokerClientAdmin) -> None:
  parts = call.data.split(":")  # ["arot", account_id, decision]
  if len(parts) != 3:
    await call.answer()
    return
  account_id, decision = parts[1], parts[2]
  if decision != "ok":
    await safe_edit_text(call.message, "Cancelled.")
    await call.answer()
    return
  result = await broker_admin.admin_rotate_token(account_id)
  if result is None:
    await safe_edit_text(call.message, f"{emojis.CROSS} Token rotation failed.")
  else:
    await safe_edit_text(call.message, messages.AdminMessages.format_rotate_result(result))
  await call.answer()


# ── /admin_help ─────────────────────────────────────────────────────
# The menu divider between the user and admin command groups is a real
# command; running it lists the admin commands.


@router.message(Command("admin_help"))
async def cmd_admin_help(message: Message) -> None:
  await message.answer(messages.AdminMessages.ADMIN_HELP)


# ── /admin_uuid ─────────────────────────────────────────────────────
# Show the internal row UUID(s) of accounts (the id the admin link-account and
# broker-side calls address). Optional arg filters to one bare account_id.


@router.message(Command("admin_uuid", "auuid"))
async def cmd_admin_uuid(
  message: Message, command: CommandObject, broker_admin: BrokerClientAdmin
) -> None:
  accounts = await broker_admin.admin_list_accounts()
  if accounts is None:
    await message.answer(f"{emojis.WARNING} Failed to fetch account list.")
    return
  arg = (command.args or "").strip()
  if arg:
    accounts = [a for a in accounts if a.get("account_id") == arg]
  await message.answer(messages.AdminMessages.format_account_uuids(accounts))


# ── /admin_linkaccount ──────────────────────────────────────────────
# Admin binds a Telegram user to an account directly (no invite token). Pick
# the account (resolved by its unambiguous UUID), then type the Telegram id.


@router.message(Command("admin_linkaccount", "linkaccount"))
async def cmd_admin_linkaccount(
  message: Message, state: FSMContext, broker_admin: BrokerClientAdmin
) -> None:
  await state.clear()
  accounts = await broker_admin.admin_list_accounts()
  if not accounts:
    await message.answer("No accounts.")
    return
  await message.answer(
    "Choose an account to link a Telegram user to:",
    reply_markup=inline.accounts_uuid_picker(accounts, "alink"),
  )


@router.callback_query(F.data.startswith("alink:"))
async def cb_admin_linkaccount_pick(call: CallbackQuery, state: FSMContext) -> None:
  account_uuid = call.data.split(":", 1)[1]
  await state.update_data(alink_account_uuid=account_uuid)
  await state.set_state(AdminLinkAccount.waiting_for_telegram_id)
  await safe_edit_text(
    call.message,
    "Send the <b>Telegram user id</b> (numeric) to link to account "
    f"<code>{html.escape(account_uuid)}</code>:",
    None,
  )
  await call.answer()


@router.message(AdminLinkAccount.waiting_for_telegram_id, F.text & ~F.text.startswith("/"))
async def receive_link_telegram_id(
  message: Message, state: FSMContext, broker_admin: BrokerClientAdmin
) -> None:
  raw = (message.text or "").strip()
  if not raw.isdigit():
    await message.answer(
      f"{emojis.WARNING} Please send a numeric Telegram user id."
    )
    return

  data = await state.get_data()
  account_uuid = data.get("alink_account_uuid")
  if not account_uuid:
    await state.clear()
    await message.answer(
      f"{emojis.CROSS} Session expired. Run /admin_linkaccount again."
    )
    return

  account = await broker_admin.admin_link_telegram(account_uuid, int(raw))
  await state.clear()
  if account is None:
    await message.answer(
      f"{emojis.CROSS} Failed to link (account not found?). "
      "Run /admin_linkaccount to retry."
    )
    return
  await message.answer(
    messages.AdminMessages.format_linked_account(account, int(raw))
  )


@router.message(AdminLinkAccount.waiting_for_telegram_id, ~F.text)
async def prompt_link_telegram_id_text(message: Message) -> None:
  await message.answer(f"{emojis.WARNING} Please send the Telegram user id as text.")


# ── /newaccount ─────────────────────────────────────────────────────
# Pre-register an account (market + gateway + admin-typed suffix) before it
# has traded, so a link token can be issued right away. account_id itself
# stays bare in the DB — market/gateway are separate fields; the picker just
# spares the admin from typing/misformatting the <market>-<gateway>- prefix.


@router.message(Command("admin_newaccount", "newaccount"))
async def cmd_newaccount(message: Message, state: FSMContext) -> None:
  await state.clear()
  await message.answer(
    f"{emojis.FOLDER} <b>New account</b>\n\nChoose a market:",
    reply_markup=inline.market_picker(),
  )


@router.callback_query(F.data.startswith("nacc:m:"))
async def cb_newaccount_market(call: CallbackQuery) -> None:
  market = call.data.split(":", 2)[2]
  if market not in MARKETS:
    await call.answer()
    return
  await safe_edit_text(
    call.message,
    f"Market: <b>{market}</b>\n\nChoose a gateway:",
    inline.gateway_picker(market),
  )
  await call.answer()


@router.callback_query(F.data.startswith("nacc:g:"))
async def cb_newaccount_gateway(
  call: CallbackQuery, state: FSMContext, broker_admin: BrokerClientAdmin
) -> None:
  parts = call.data.split(":")  # ["nacc", "g", market, gateway]
  market = parts[2] if len(parts) > 2 else ""
  gateway = parts[3] if len(parts) > 3 else ""
  if gateway not in GATEWAYS_BY_MARKET.get(market, []):
    await call.answer()
    return

  accounts = await broker_admin.admin_list_accounts() or []
  existing = [
    a
    for a in accounts
    if a.get("market") == market and a.get("gateway") == gateway
  ]

  await state.update_data(market=market, gateway=gateway)
  await state.set_state(CreateAccount.waiting_for_account_id)

  text = f"Market: <b>{market}</b> · Gateway: <b>{gateway}</b>\n\n"
  if existing:
    ids = ", ".join(f"<code>{html.escape(str(a.get('account_id')))}</code>" for a in existing)
    text += f"Existing account_id(s) for this pair: {ids}\n\n"
  text += (
    "Send the account_id to register (no market/gateway prefix — just the "
    "raw id, no ':' or spaces):"
  )
  await safe_edit_text(call.message, text, None)
  await call.answer()


@router.message(CreateAccount.waiting_for_account_id, F.text & ~F.text.startswith("/"))
async def receive_new_account_id(
  message: Message, state: FSMContext, broker_admin: BrokerClientAdmin
) -> None:
  raw = (message.text or "").strip()
  if not raw or len(raw) > 50 or any(ch.isspace() or ch == ":" for ch in raw):
    await message.answer(
      f"{emojis.WARNING} Invalid account_id (no ':' or spaces, max 50 chars). Please try again."
    )
    return

  data = await state.get_data()
  market, gateway = data.get("market"), data.get("gateway")
  if not market or not gateway:
    await state.clear()
    await message.answer(f"{emojis.CROSS} Session expired. Run /newaccount again.")
    return

  account = await broker_admin.admin_create_account(raw, market, gateway)
  await state.clear()
  if account is None:
    await message.answer(
      f"{emojis.CROSS} Failed to create account (it may already exist). Run /newaccount to retry."
    )
    return
  await message.answer(messages.AdminMessages.format_account_created(account))


@router.message(CreateAccount.waiting_for_account_id, ~F.text)
async def prompt_new_account_id_text(message: Message) -> None:
  await message.answer(f"{emojis.WARNING} Please send the account_id as text.")


# ── /settings ───────────────────────────────────────────────────────


async def _render_settings(
  broker_admin: BrokerClientAdmin,
) -> tuple[Optional[str], Optional[InlineKeyboardMarkup]]:
  states = await broker_admin.admin_get_settings()
  if states is None:
    return None, None
  return messages.AdminMessages.format_settings(states), inline.settings_keyboard(states)


@router.message(Command("admin_settings", "settings"))
async def cmd_settings(message: Message, broker_admin: BrokerClientAdmin) -> None:
  text, kb = await _render_settings(broker_admin)
  if text is None:
    await message.answer(f"{emojis.WARNING} Failed to fetch broker settings.")
    return
  await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("aset:"))
async def cb_settings_toggle(call: CallbackQuery, broker_admin: BrokerClientAdmin) -> None:
  slug = call.data.split(":", 1)[1]
  await broker_admin.admin_toggle_setting(slug)
  text, kb = await _render_settings(broker_admin)
  if text is None:
    await call.answer("Error", show_alert=True)
    return
  await safe_edit_text(call.message, text, kb)
  await call.answer("Updated")
