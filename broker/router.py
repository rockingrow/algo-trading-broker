from fastapi import APIRouter

from broker.api.api import get_api_router
from broker.api.admin import get_admin_router
from broker.api.webhook import get_webhook_router


def get_core_router() -> APIRouter:
  """Aggregates all system routers into one core router."""
  router = APIRouter()

  # Include all sub-routers
  router.include_router(get_api_router())
  router.include_router(get_admin_router())
  router.include_router(get_webhook_router())

  return router
