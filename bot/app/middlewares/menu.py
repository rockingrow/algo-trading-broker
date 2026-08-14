"""
app/middlewares/menu.py — Keep every user's command menu in step with linking.

Registered as an outer middleware on ``dp.update``, so *every* incoming update
re-checks whether the sender currently has a linked account and re-applies
their menu when that changed. Checking on each update — rather than only when
the bot itself links or unlinks someone — is what keeps the menu honest after a
change made elsewhere: an ``/admin_rotate`` unlinks every user bound to the
account, and an ``/admin_linkaccount`` links one, neither of them from a chat
this bot can update at the time.

The resolved account is handed on in ``data["account"]`` so AuthMiddleware
doesn't ask the broker the same question a second time. The applied menu is
cached in ``CommandMenu``, so the common case (nothing changed) costs one
broker call and no Telegram call.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.services.broker_client import BrokerClientUser
from app.services.menu import CommandMenu


class CommandMenuMiddleware(BaseMiddleware):
  def __init__(self, menu: CommandMenu) -> None:
    self.menu = menu

  async def __call__(
    self,
    handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
    event: TelegramObject,
    data: dict[str, Any],
  ) -> Any:
    # Handlers reach the menu through data["menu"] to re-sync the moment they
    # link or unlink an account, instead of leaving the user with a stale menu
    # until their next message.
    data["menu"] = self.menu

    user = data.get("event_from_user")
    if user is None or user.is_bot:
      return await handler(event, data)

    broker: BrokerClientUser = data["broker"]
    answered, account = await broker.resolve_account(user.id)
    if account is not None:
      data["account"] = account
    if answered and self._is_private(data):
      await self.menu.sync(data["bot"], user.id, linked=account is not None)

    return await handler(event, data)

  @staticmethod
  def _is_private(data: dict[str, Any]) -> bool:
    """A chat-scoped menu only means anything in the bot's private chat with the
    user. Writing from a group would otherwise aim the menu at a private chat
    that may not exist, and Telegram answers "chat not found" every time."""
    chat = data.get("event_chat")
    return chat is not None and chat.type == "private"
