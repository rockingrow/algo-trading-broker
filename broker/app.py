from contextlib import asynccontextmanager

from fastapi import FastAPI

from broker.db.engine import close_db, init_db
from broker.services.publisher_service import SignalPublisher
from broker.router import get_core_router


@asynccontextmanager
async def lifespan(app: FastAPI):
  await init_db()
  publisher = SignalPublisher()
  app.state.publisher = publisher
  yield
  publisher.close()
  await close_db()


def create_app() -> FastAPI:
  """Build and return the FastAPI application with all routes wired up."""
  app = FastAPI(
    title="Algo Trading Broker",
    description=(
      "Receives TradingView JSON webhook alerts, logs them to PostgreSQL, "
      "and fans them out via ZeroMQ PUB to subscriber VPS nodes."
    ),
    version="2.0.0",
    lifespan=lifespan,
  )

  # Include Core Router
  app.include_router(get_core_router())

  return app
