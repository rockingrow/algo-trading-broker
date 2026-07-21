import asyncio
from contextlib import asynccontextmanager
import traceback

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from broker.db.engine import close_db, init_db
from broker.db.repository import (
  SqlAlchemyAccountRepository,
  SqlAlchemySettingRepository,
  SqlAlchemySignalRepository,
  SqlAlchemyTradeBroadcastRepository,
  SqlAlchemyTradeRepository,
)
from broker.helpers import emoji_constants as em
from broker.logger import get_logger
from broker.nats import nats_client
from broker.openapi import fastapi_kwargs
from broker.providers import make_signals_notifier
from broker.router import get_core_router
from broker.services.nats_service import (
  NatsPublisher,
  SystemEventConsumer,
  TradeEventConsumer,
)
from broker.services.notification_service import TelegramNotification
from broker.services.signal_processing_service import (
  SignalProcessingService,
  SignalWorker,
)
from broker.services.signal_retry_job import SignalRetryJob
from broker.services.trade_broadcast_service import TradeBroadcastService
from broker.settings import settings

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
  notifier = TelegramNotification()

  # Start the background worker that forwards ERROR logs to Telegram. Cheap and
  # idempotent when the feature is disabled (no records ever reach the handler).
  if settings.telegram.ENABLED and settings.telegram.LOG_ERRORS_ENABLED:
    from broker.services.notification_service import telegram_log_handler

    telegram_log_handler.start(asyncio.get_running_loop())

  await init_db()
  nats_client.set_notifier(notifier)
  await nats_client.connect()

  publisher = NatsPublisher(connection=nats_client)
  setting_repo = SqlAlchemySettingRepository()
  signal_repo = SqlAlchemySignalRepository()
  trade_broadcast_service = TradeBroadcastService(
    broadcast_repository=SqlAlchemyTradeBroadcastRepository(),
    setting_repository=setting_repo,
  )
  consumer = TradeEventConsumer(
    trade_repository=SqlAlchemyTradeRepository(),
    connection=nats_client,
    broadcast_service=trade_broadcast_service,
  )
  system_consumer = SystemEventConsumer(
    setting_repository=setting_repo,
    account_repository=SqlAlchemyAccountRepository(),
    publisher=publisher,
    signal_repository=signal_repo,
    connection=nats_client,
  )
  signal_service = SignalProcessingService(
    signal_repository=signal_repo,
    setting_repository=setting_repo,
    publisher=publisher,
    notifier=make_signals_notifier(setting_repo),
    webhook_secret=settings.webhook.SECRET,
  )
  signal_worker = SignalWorker(service=signal_service, connection=nats_client)
  signal_retry_job = SignalRetryJob(
    service=signal_service,
    signal_repository=signal_repo,
    interval_seconds=settings.signal.RETRY_INTERVAL_SECONDS,
  )
  await consumer.start()
  await system_consumer.start()
  await signal_worker.start()
  await signal_retry_job.start()
  app.state.publisher = publisher

  api_prefix = f"/{settings.broker_api.API_PREFIX}" if settings.broker_api.API_PREFIX else ""

  # Notification: Startup
  await notifier.send_message(
    f"{em.BROKER_STARTED} <b>Broker Node Started</b>\n"
    f"{em.PLUG} NATS Publishing: <code>{nats_client.subjects_line()}</code> + dynamic (by strategy & per-account ADMIN)\n"
    f"{em.PLUG} NATS Listening: <code>{nats_client.listen_subjects_line()}</code>\n"
    f"{em.ENDPOINT} Endpoint: <code>{settings.broker_url}{api_prefix}</code>"
  )

  yield

  # Notification: Shutdown
  await notifier.send_message(
    f"{em.BROKER_STOPPED} <b>Broker Node Stopped</b>\n"
    f"{em.ENDPOINT} Endpoint: <code>{settings.broker_url}{api_prefix}</code>"
  )

  await signal_retry_job.stop()
  await signal_worker.stop()
  await system_consumer.stop()
  await consumer.stop()
  await nats_client.close()
  await close_db()

  if settings.telegram.ENABLED and settings.telegram.LOG_ERRORS_ENABLED:
    from broker.services.notification_service import telegram_log_handler

    await telegram_log_handler.stop()


def install_webhook_connection_close(app: FastAPI) -> None:
  """Send ``Connection: close`` on every response to the TradingView webhook.

  TradingView keeps a per-endpoint TCP pool, but uvicorn drops idle keep-alive
  sockets after ``WEBHOOK_KEEPALIVE_TIMEOUT`` seconds. On any strategy whose
  alert cadence exceeds that timeout (e.g. a 15-minute timeframe against the
  120s default), TradingView reuses a socket the server has already closed
  and the delivery fails with "server closed the connection unexpectedly".
  Signalling close per response makes TradingView open a fresh TCP for every
  alert, removing the race entirely at the cost of one extra handshake.
  """

  @app.middleware("http")
  async def _force_close_webhook(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.endswith("/secret/webhook"):
      response.headers["Connection"] = "close"
    return response


def create_app() -> FastAPI:
  """Build and return the FastAPI application with all routes wired up."""
  app = FastAPI(lifespan=lifespan, **fastapi_kwargs())

  install_webhook_connection_close(app)

  @app.exception_handler(RequestValidationError)
  async def validation_exception_handler(request: Request, exc: RequestValidationError):
    log.warning(
      "422 Unprocessable Content | %s %s | %s",
      request.method,
      request.url.path,
      exc.errors(),
    )
    return JSONResponse(status_code=422, content={"detail": exc.errors()})

  @app.exception_handler(Exception)
  async def global_exception_handler(request: Request, exc: Exception):
    log.error("Unhandled exception on %s %s: %s", request.method, request.url.path, exc)
    log.error(traceback.format_exc())
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})

  # Include Core Router — mount under secret prefix if configured
  api_prefix = f"/{settings.broker_api.API_PREFIX}" if settings.broker_api.API_PREFIX else ""
  app.include_router(get_core_router(), prefix=api_prefix)

  return app
