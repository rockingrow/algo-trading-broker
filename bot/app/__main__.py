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
from aiogram.types import BotCommand

from app.config import settings
from app.handlers import get_routers
from app.logger import get_logger
from app.middlewares.deps import DepsMiddleware
from app.services.broker_client import BrokerClient

log = get_logger("bot")

_COMMANDS = [
  BotCommand(command="start", description="Liên kết tài khoản"),
  BotCommand(command="trades", description="Giao dịch gần đây"),
  BotCommand(command="flat", description="Đóng toàn bộ vị thế"),
  BotCommand(command="prevent", description="Chặn vào lệnh mới"),
  BotCommand(command="allow", description="Cho phép vào lệnh mới"),
  BotCommand(command="status", description="Thông tin tài khoản"),
  BotCommand(command="unlink", description="Hủy liên kết"),
  BotCommand(command="help", description="Trợ giúp"),
]


async def main() -> None:
  bot = Bot(
    token=settings.TELEGRAM_BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
  )
  broker = BrokerClient(
    base_url=settings.BOT_BROKER_BASE_URL,
    api_key=settings.BROKER_API_KEY,
    api_prefix=settings.BROKER_API_PREFIX,
    timeout=settings.BOT_REQUEST_TIMEOUT,
  )

  dp = Dispatcher(storage=MemoryStorage())
  dp.update.outer_middleware(DepsMiddleware(broker))
  for router in get_routers():
    dp.include_router(router)

  async def on_startup() -> None:
    log.info("Bot starting — broker base_url=%s", settings.BOT_BROKER_BASE_URL)
    await bot.set_my_commands(_COMMANDS)

  async def on_shutdown() -> None:
    log.info("Bot shutting down — closing resources")
    await broker.aclose()
    await bot.session.close()

  dp.startup.register(on_startup)
  dp.shutdown.register(on_shutdown)

  try:
    await dp.start_polling(bot)
  finally:
    # Safety net in case polling exits before the shutdown hook runs.
    await broker.aclose()


if __name__ == "__main__":
  try:
    asyncio.run(main())
  except (KeyboardInterrupt, SystemExit):
    log.info("Bot stopped")
