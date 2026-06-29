"""
app/middlewares/deps.py — Dependency injection middleware.

Injects the shared ``BrokerClient`` into every handler's ``data`` so handlers
declare ``broker: BrokerClient`` as a parameter instead of reaching for a
global. Registered as an outer middleware on ``dp.update`` so it runs for both
messages and callback queries.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.services.broker_client import BrokerClient


class DepsMiddleware(BaseMiddleware):
  def __init__(self, broker: BrokerClient) -> None:
    self.broker = broker

  async def __call__(
    self,
    handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
    event: TelegramObject,
    data: dict[str, Any],
  ) -> Any:
    data["broker"] = self.broker
    return await handler(event, data)
