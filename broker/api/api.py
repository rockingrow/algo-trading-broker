from typing import Dict, List

from fastapi import APIRouter, Depends

from broker.security.ensure_api_key import ensure_api_key
from broker.db.repository import get_accounts
from broker.schemas.account_schema import AccountResponse
from broker.logger import get_logger

log = get_logger(__name__)


def get_api_router() -> APIRouter:
  router = APIRouter()

  @router.get("/health", tags=["system"])
  async def health() -> Dict[str, str]:
    return {"status": "ok"}

  @router.get(
    "/accounts",
    tags=["accounts"],
    response_model=List[AccountResponse],
    dependencies=[Depends(ensure_api_key)],
  )
  async def list_accounts() -> List[AccountResponse]:
    """Return all accounts ordered by last activity descending."""
    accounts = await get_accounts()
    return [AccountResponse.model_validate(a) for a in accounts]

  return router
