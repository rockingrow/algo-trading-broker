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

from app.commands import setup_bot_commands
from app.config import settings
from app.handlers import get_routers
from app.logger import get_logger
from app.middlewares.deps import DepsMiddleware
from app.services.broker_client import BrokerClientAdmin, BrokerClientUser

log = get_logger("bot")


async def main() -> None:
  bot = Bot(
    token=settings.TELEGRAM_BOT_TOKEN,
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

  dp = Dispatcher(storage=MemoryStorage())
  dp.update.outer_middleware(DepsMiddleware(broker, broker_admin))
  for router in get_routers():
    dp.include_router(router)

  async def on_startup() -> None:
    admin_ids = settings.admin_ids
    log.info(
      "Bot starting — broker base_url=%s, admins=%d",
      settings.BOT_BROKER_BASE_URL,
      len(admin_ids),
    )
    await setup_bot_commands(bot, admin_ids)

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
