"""
app/__main__.py — Bot entrypoint.

Builds the Dispatcher, wires dependency-injection + routers, sets the command
menu, and starts long-polling. aiogram handles SIGINT/SIGTERM and drains
in-flight handlers; the shutdown hook closes the HTTP client and bot session
for a graceful exit.
"""

from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from app.config import settings
from app.handlers import get_routers
from app.logger import get_logger
from app.middlewares.deps import DepsMiddleware
from app.middlewares.menu import CommandMenuMiddleware
from app.services.broker_client import BrokerClientAdmin, BrokerClientUser
from app.services.menu import CommandMenu

log = get_logger("bot")


async def main() -> None:
  bot = Bot(
    token=settings.BOT_TELEGRAM_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
  )
  broker_kwargs = dict(
    base_url=settings.BOT_BROKER_BASE_URL,
    api_key=settings.BROKER_API_KEY,
    api_prefix=settings.BROKER_API_PREFIX,
    timeout=settings.BOT_REQUEST_TIMEOUT,
  )
  broker = BrokerClientUser(**broker_kwargs)
  broker_admin = BrokerClientAdmin(**broker_kwargs)

  admin_ids = settings.admin_ids
  menu = CommandMenu(admin_ids)

  dp = Dispatcher(storage=MemoryStorage())
  dp.update.outer_middleware(DepsMiddleware(broker, broker_admin))
  # After DepsMiddleware: it needs the broker client to resolve the sender's
  # account, and hands that account on to AuthMiddleware.
  dp.update.outer_middleware(CommandMenuMiddleware(menu))
  for router in get_routers():
    dp.include_router(router)

  async def on_startup() -> None:
    log.info(
      "Bot starting — broker base_url=%s, admins=%d",
      settings.BOT_BROKER_BASE_URL,
      len(admin_ids),
    )
    await menu.setup(bot, broker)

  async def on_shutdown() -> None:
    log.info("Bot shutting down — closing resources")
    await broker.aclose()
    await broker_admin.aclose()
    await bot.session.close()

  dp.startup.register(on_startup)
  dp.shutdown.register(on_shutdown)

  try:
    await dp.start_polling(bot)
  finally:
    # Safety net in case polling exits before the shutdown hook runs.
    await broker.aclose()
    await broker_admin.aclose()


if __name__ == "__main__":
  try:
    asyncio.run(main())
  except (KeyboardInterrupt, SystemExit):
    log.info("Bot stopped")
