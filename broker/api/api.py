from typing import Dict, List

from fastapi import APIRouter, Depends

from broker.providers import get_account_repository
from broker.interfaces import AccountRepository
from broker.logger import get_logger
from broker.openapi import AUTH_RESPONSES
from broker.schemas.account_schema import AccountResponse
from broker.security.ensure_api_key import ensure_api_key

log = get_logger(__name__)


def get_api_router() -> APIRouter:
  router = APIRouter()

  @router.get("/health", tags=["system"], summary="Liveness probe")
  async def health() -> Dict[str, str]:
    """Return ``{"status": "ok"}`` while the process is alive. No auth required."""
    return {"status": "ok"}

  @router.get(
    "/accounts",
    tags=["accounts"],
    summary="List trading accounts",
    response_model=List[AccountResponse],
    dependencies=[Depends(ensure_api_key)],
    responses=AUTH_RESPONSES,
  )
  async def list_accounts(
    accounts_repo: AccountRepository = Depends(get_account_repository),
  ) -> List[AccountResponse]:
    """Return all accounts ordered by last activity descending."""
    accounts = await accounts_repo.get_all()
    return [AccountResponse.model_validate(a) for a in accounts]

  return router
