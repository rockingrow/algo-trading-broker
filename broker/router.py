from fastapi import APIRouter

from broker.apis.api import get_router as get_api_router
from broker.apis.trade import get_trade_router
from broker.apis.webhook import get_webhook_router


def get_core_router() -> APIRouter:
  """Aggregates all system routers into one core router."""
  router = APIRouter()

  # Include all sub-routers
  router.include_router(get_api_router())
  router.include_router(get_trade_router())
  router.include_router(get_webhook_router())

  return router
