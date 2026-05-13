from contextlib import asynccontextmanager
import traceback

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from broker.db.engine import close_db, init_db
from broker.services.publisher_service import NatsPublisher
from broker.services.trade_listener import NatsTradeListener
from broker.router import get_core_router
from broker.logger import get_logger

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
  from broker.services.notification_service import TelegramNotification

  await init_db()
  publisher = NatsPublisher()
  await publisher.connect()
  app.state.publisher = publisher

  trade_listener = NatsTradeListener()
  await trade_listener.start()
  app.state.trade_listener = trade_listener

  # Notification: Startup
  TelegramNotification().send_message("🟢 Broker Node Started")

  yield

  # Notification: Shutdown
  TelegramNotification().send_message("🛑 Broker Node Stopped")

  await trade_listener.stop()
  await publisher.close()
  await close_db()


def create_app() -> FastAPI:
  """Build and return the FastAPI application with all routes wired up."""
  app = FastAPI(
    title="Algo Trading Broker",
    description=(
      "Receives TradingView JSON webhook alerts, logs them to PostgreSQL, "
      "and fans them out via NATS to subscriber VPS nodes."
    ),
    version="2.0.0",
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
