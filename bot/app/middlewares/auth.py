"""
app/middlewares/auth.py — "Require linked account" guard.

Applied to routers whose commands need an authenticated account (trades,
control commands, account management). Resolves the account bound to the
Telegram user via the broker and injects it into ``data["account"]``. If the
user has not linked yet, it replies with a hint and stops propagation.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.services.broker_client import BrokerClientUser

_NOT_LINKED = "You haven't linked an account yet. Type /start to link using your UUID code."


class AuthMiddleware(BaseMiddleware):
  async def __call__(
    self,
    handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
    event: TelegramObject,
    data: dict[str, Any],
  ) -> Any:
    broker: BrokerClientUser = data["broker"]
    user = data.get("event_from_user")
    if user is None:
      return await handler(event, data)

    account = await broker.get_account(user.id)
    if account is None:
      await self._deny(event)
      return None

    data["account"] = account
    return await handler(event, data)

  @staticmethod
  async def _deny(event: TelegramObject) -> None:
    if isinstance(event, CallbackQuery):
      await event.answer(_NOT_LINKED, show_alert=True)
    elif isinstance(event, Message):
      await event.answer(_NOT_LINKED)
