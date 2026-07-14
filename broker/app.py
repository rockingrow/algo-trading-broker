import asyncio
from contextlib import asynccontextmanager
import traceback

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from broker.db.engine import close_db, init_db
from broker.db.repository import (
  SqlAlchemyAccountRepository,
  SqlAlchemySettingRepository,
  SqlAlchemyTradeRepository,
)
from broker.helpers import emoji_constants as em
from broker.logger import get_logger
from broker.nats import nats_client
from broker.openapi import fastapi_kwargs
from broker.router import get_core_router
from broker.services.nats_publisher import NatsPublisher
from broker.services.nats_service import SystemEventConsumer, TradeEventConsumer
from broker.services.notification_service import TelegramNotification
from broker.settings import settings

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
  notifier = TelegramNotification()

  # Start the background worker that forwards ERROR logs to Telegram. Cheap and
  # idempotent when the feature is disabled (no records ever reach the handler).
  if settings.TELEGRAM_ENABLED and settings.TELEGRAM_LOG_ERRORS_ENABLED:
    from broker.services.notification_service import telegram_log_handler

    telegram_log_handler.start(asyncio.get_running_loop())

  await init_db()
  nats_client.set_notifier(notifier)
  await nats_client.connect()

  publisher = NatsPublisher(connection=nats_client)
  consumer = TradeEventConsumer(
    trade_repository=SqlAlchemyTradeRepository(), connection=nats_client
  )
  system_consumer = SystemEventConsumer(
    setting_repository=SqlAlchemySettingRepository(),
    account_repository=SqlAlchemyAccountRepository(),
    publisher=publisher,
    connection=nats_client,
  )
  await consumer.start()
  await system_consumer.start()
  app.state.publisher = publisher

  api_prefix = f"/{settings.BROKER_API_PREFIX}" if settings.BROKER_API_PREFIX else ""

  # Notification: Startup
  await notifier.send_message(
    f"{em.BROKER_STARTED} <b>Broker Node Started</b>\n"
    f"{em.PLUG} NATS Publishing: <code>{nats_client.subjects_line()}</code> + dynamic (by strategy)\n"
    f"{em.PLUG} NATS Listening: <code>{nats_client.listen_subjects_line()}</code>\n"
    f"{em.ENDPOINT} Endpoint: <code>{settings.broker_url}{api_prefix}</code>"
  )

  yield

  # Notification: Shutdown
  await notifier.send_message(
    f"{em.BROKER_STOPPED} <b>Broker Node Stopped</b>\n"
    f"{em.ENDPOINT} Endpoint: <code>{settings.broker_url}{api_prefix}</code>"
  )

  await system_consumer.stop()
  await consumer.stop()
  await nats_client.close()
  await close_db()

  if settings.TELEGRAM_ENABLED and settings.TELEGRAM_LOG_ERRORS_ENABLED:
    from broker.services.notification_service import telegram_log_handler

    await telegram_log_handler.stop()


def create_app() -> FastAPI:
  """Build and return the FastAPI application with all routes wired up."""
  app = FastAPI(lifespan=lifespan, **fastapi_kwargs())

  @app.exception_handler(Exception)
  async def global_exception_handler(request: Request, exc: Exception):
    log.error("Unhandled exception on %s %s: %s", request.method, request.url.path, exc)
    log.error(traceback.format_exc())
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})

  # Include Core Router — mount under secret prefix if configured
  api_prefix = f"/{settings.BROKER_API_PREFIX}" if settings.BROKER_API_PREFIX else ""
  app.include_router(get_core_router(), prefix=api_prefix)

  return app
