from contextlib import asynccontextmanager
import traceback

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from broker.db.engine import close_db, init_db
from broker.nats import nats_client
from broker.services.notification_service import TelegramNotification
from broker.services.nats_service import NatsService
from broker.router import get_core_router
from broker.logger import get_logger
from broker.settings import settings

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
  await init_db()
  await nats_client.connect()
  nats_service = NatsService()
  await nats_service.start()
  app.state.publisher = nats_service

  # Notification: Startup
  TelegramNotification().send_message(
    f"🟢 <b>Broker Node Started</b>\n"
    f"🔌 NATS Publishing: <code>{nats_client._subjects_line()}</code> + dynamic (by strategy)\n"
    f"🔌 NATS Listening: <code>{nats_client.LISTEN_SUBJECT.value}</code>\n"
    f"🌐 Endpoint: <code>{settings.broker_url}</code>"
  )

  yield

  # Notification: Shutdown
  TelegramNotification().send_message(
    f"🛑 <b>Broker Node Stopped</b>\n🌐 Endpoint: <code>{settings.broker_url}</code>"
  )

  await nats_service.stop()
  await nats_client.close()
  await close_db()


def create_app() -> FastAPI:
  """Build and return the FastAPI application with all routes wired up."""
  app = FastAPI(
    title="Algo Trading Broker",
    description=(
      "Receives TradingView JSON webhook alerts, logs them to PostgreSQL, "
      "and fans them out via NATS to subscriber VPS nodes."
    ),
    version="1.0.0",
    lifespan=lifespan,
  )

  @app.exception_handler(Exception)
  async def global_exception_handler(request: Request, exc: Exception):
    log.error("Unhandled exception on %s %s: %s", request.method, request.url.path, exc)
    log.error(traceback.format_exc())
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})

  # Include Core Router
  app.include_router(get_core_router())

  return app
