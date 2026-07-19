import asyncio
from typing import Dict, List, Literal

from fastapi import APIRouter, Depends, Query

from broker.providers import get_account_repository, get_trade_repository
from broker.interfaces import AccountRepository, TradeRepository
from broker.logger import get_logger
from broker.openapi import AUTH_RESPONSES
from broker.schemas.account_schema import AccountResponse
from broker.schemas.trade_schema import PageMeta, TradeListResponse, TradeResponse
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
    """Return all accounts ordered by last activity descending, each with its
    current link token and linked bot users joined in from their own tables."""
    accounts = await accounts_repo.get_all()
    summaries = await accounts_repo.get_link_summaries([a.id for a in accounts])
    result: List[AccountResponse] = []
    for a in accounts:
      resp = AccountResponse.model_validate(a)
      summary = summaries.get(a.id)
      if summary is not None:
        resp.link_token = summary.link_token
        resp.linked_user_ids = summary.linked_user_ids
      result.append(resp)
    return result

  @router.get(
    "/{account_id}/trades",
    tags=["trades"],
    summary="List trades for an account",
    response_model=TradeListResponse,
    dependencies=[Depends(ensure_api_key)],
    responses=AUTH_RESPONSES,
  )
  async def list_trades(
    account_id: str,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    order: Literal["asc", "desc"] = Query("desc"),
    order_by: Literal["updatedAt", "createdAt", "status", "symbol"] = Query(
      "updatedAt"
    ),
    trades_repo: TradeRepository = Depends(get_trade_repository),
  ) -> TradeListResponse:
    """Return trades for the given account with pagination metadata."""
    trades, total = await asyncio.gather(
      trades_repo.list_by_account(
        account_id, limit=limit, offset=offset, order=order, order_by=order_by
      ),
      trades_repo.count_by_account(account_id),
    )
    return TradeListResponse(
      data=[TradeResponse.model_validate(t) for t in trades],
      page=PageMeta(
        total=total, limit=limit, offset=offset, order=order, order_by=order_by
      ),
    )

  return router
