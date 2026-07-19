"""
app/middlewares/deps.py — Dependency injection middleware.

Injects the shared broker clients into every handler's ``data`` so handlers
declare ``broker: BrokerClientUser`` and/or ``broker_admin: BrokerClientAdmin``
as parameters instead of reaching for a global. Registered as an outer
middleware on ``dp.update`` so it runs for both messages and callback queries.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.services.broker_client import BrokerClientAdmin, BrokerClientUser


class DepsMiddleware(BaseMiddleware):
  def __init__(self, broker: BrokerClientUser, broker_admin: BrokerClientAdmin) -> None:
    self.broker = broker
    self.broker_admin = broker_admin

  async def __call__(
    self,
    handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
    event: TelegramObject,
    data: dict[str, Any],
  ) -> Any:
    data["broker"] = self.broker
    data["broker_admin"] = self.broker_admin
    return await handler(event, data)
