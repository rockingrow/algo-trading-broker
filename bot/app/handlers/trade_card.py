"""
app/handlers/trade_card.py — Buttons on the broker's live trade cards.

The card is a DM the *broker* posts when one of a subscriber's accounts opens a
trade, and edits in place as that trade progresses. It is sent with this bot's
token, so its buttons produce ordinary callback queries on this bot's long
poll — which is what this router answers:

- ``tc:d``  Detail  — re-render the card with the strategy/account/risk block,
- ``tc:s``  Summary — collapse it again,
- ``tc:x``  Exit    — replace the card with a confirmation prompt,
- ``tc:xy`` / ``tc:xn`` — publish the close, or restore the card.

Every handler re-fetches the trade rather than trusting what the message
happens to show: the broker may have refreshed the card between the render and
the tap, and the fetch doubles as the authorisation check.

**Not on the protected router, by design.** ``AuthMiddleware`` resolves the
caller's *active* account and turns away anyone without one, but a card lives
in the chat long after its owner has switched accounts — gating on the active
account would break the buttons on every card but the current account's. The
broker authorises each call against *every* account linked to the caller
instead (``get_own_trade``), which is the right check and also the reason an
unlinked user simply gets "not available" here.
"""

from __future__ import annotations

from typing import Optional

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from app import emojis
from app.constants import (
  CB_TRADE_DETAIL,
  CB_TRADE_EXIT,
  CB_TRADE_EXIT_CANCEL,
  CB_TRADE_EXIT_CONFIRM,
  CB_TRADE_SUMMARY,
)
from app.keyboards import inline
from app.presenters import trade_card as card
from app.services.broker_client import BrokerClientAdmin, BrokerClientUser
from app.utils.telegram import safe_edit_text
from app.utils.timezone import offset_hours_from_payload

router = Router(name="trade_card")

_UNAVAILABLE = "This trade is no longer available."
_EXIT_FAILED = "Couldn't send the close order. The trade may already be closed."


def _trade_id(data: str) -> Optional[str]:
  """The uuid tail of ``"<prefix>:<uuid>"``, or None if the data is malformed."""
  parts = data.split(":")
  return parts[2] if len(parts) == 3 and parts[2] else None


async def _render(
  broker: BrokerClientUser,
  broker_admin: BrokerClientAdmin,
  telegram_user_id: int,
  trade_id: str,
  *,
  detailed: bool = False,
  footer: Optional[str] = None,
) -> tuple[Optional[str], Optional[InlineKeyboardMarkup]]:
  """Fetch the trade and render its card, or ``(None, None)`` when it's gone."""
  trade = await broker.get_trade(telegram_user_id, trade_id)
  if trade is None:
    return None, None
  tz_offset = offset_hours_from_payload(await broker_admin.get_notification_timezone())
  closed = card.is_closed(trade)
  return (
    card.format_trade_card(trade, tz_offset, detailed=detailed, footer=footer),
    inline.trade_card(trade_id, detailed=detailed, closed=closed),
  )


async def _show(
  call: CallbackQuery,
  broker: BrokerClientUser,
  broker_admin: BrokerClientAdmin,
  *,
  detailed: bool,
  footer: Optional[str] = None,
  alert: Optional[str] = None,
) -> None:
  """Re-render the card this callback came from, then answer the query."""
  trade_id = _trade_id(call.data)
  if trade_id is None:
    await call.answer()
    return

  text, markup = await _render(
    broker,
    broker_admin,
    call.from_user.id,
    trade_id,
    detailed=detailed,
    footer=footer,
  )
  if text is None:
    await call.answer(_UNAVAILABLE, show_alert=True)
    return

  await safe_edit_text(call.message, text, markup)
  await call.answer(alert, show_alert=alert is not None)


@router.callback_query(F.data.startswith(f"{CB_TRADE_DETAIL}:"))
async def cb_detail(
  call: CallbackQuery, broker: BrokerClientUser, broker_admin: BrokerClientAdmin
) -> None:
  await _show(call, broker, broker_admin, detailed=True)


@router.callback_query(F.data.startswith(f"{CB_TRADE_SUMMARY}:"))
async def cb_summary(
  call: CallbackQuery, broker: BrokerClientUser, broker_admin: BrokerClientAdmin
) -> None:
  await _show(call, broker, broker_admin, detailed=False)


@router.callback_query(F.data.startswith(f"{CB_TRADE_EXIT}:"))
async def cb_exit(call: CallbackQuery, broker: BrokerClientUser) -> None:
  """Swap the card for a confirmation prompt.

  Replacing the body (not just the keyboard) means Cancel has one card to go
  back to, whichever view the user was in when they tapped Exit.
  """
  trade_id = _trade_id(call.data)
  if trade_id is None:
    await call.answer()
    return

  trade = await broker.get_trade(call.from_user.id, trade_id)
  if trade is None:
    await call.answer(_UNAVAILABLE, show_alert=True)
    return
  if card.is_closed(trade):
    await call.answer("This trade is already closed.", show_alert=True)
    return

  await safe_edit_text(
    call.message, card.format_exit_prompt(trade), inline.trade_exit_confirm(trade_id)
  )
  await call.answer()


@router.callback_query(F.data.startswith(f"{CB_TRADE_EXIT_CANCEL}:"))
async def cb_exit_cancel(
  call: CallbackQuery, broker: BrokerClientUser, broker_admin: BrokerClientAdmin
) -> None:
  await _show(call, broker, broker_admin, detailed=False)


@router.callback_query(F.data.startswith(f"{CB_TRADE_EXIT_CONFIRM}:"))
async def cb_exit_confirm(
  call: CallbackQuery, broker: BrokerClientUser, broker_admin: BrokerClientAdmin
) -> None:
  """Publish the close, then put the card back with the outcome on it.

  The trade is still open at this point — the worker has to act on the FLAT and
  report back — so the card keeps its buttons and gains a footer saying the
  exit is in flight. The broker overwrites both the moment the close lands.

  A failed call re-renders the card too rather than leaving the prompt up: the
  usual reason is that the trade closed between the prompt and the tap, and the
  refreshed card is the answer to that.
  """
  trade_id = _trade_id(call.data)
  if trade_id is None:
    await call.answer()
    return

  result = await broker.exit_trade(call.from_user.id, trade_id)
  await _show(
    call,
    broker,
    broker_admin,
    detailed=False,
    footer=card.EXIT_REQUESTED_FOOTER if result is not None else None,
    alert=None if result is not None else _EXIT_FAILED,
  )


@router.callback_query(F.data.startswith("tc:"))
async def cb_unknown(call: CallbackQuery) -> None:
  """Catch-all for a card built by a newer broker than this bot.

  Without it the tap would spin until Telegram times the query out; with it the
  user gets told plainly that the button needs a newer bot.
  """
  await call.answer(
    f"{emojis.WARNING} This button isn't supported by the running bot version.",
    show_alert=True,
  )
