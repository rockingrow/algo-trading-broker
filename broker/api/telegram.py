"""
broker/api/telegram.py
──────────────────────
HTTP surface consumed by the Telegram bot service (``/v1/telegram/*``).

These endpoints are the first slice of the future "User APIs". They are all
guarded by ``ensure_api_key`` because the only caller is the trusted bot
service: the end-user's identity is the ``telegram_user_id`` taken from a
verified Telegram update and passed through by the bot — it is never derived
from user-typed text.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status

from broker.db.models import Account
from broker.interfaces import (
  AccountRepository,
  SignalPublisher,
  TradeBroadcastRepository,
  TradeRepository,
)
from broker.logger import get_logger
from broker.openapi import AUTH_RESPONSES
from broker.providers import (
  get_account_repository,
  get_publisher,
  get_trade_broadcast_repository,
  get_trade_repository,
)
from broker.schemas.publisher_schema import AdminActionEnum
from broker.schemas.telegram_schema import (
  BroadcastSubscriptionResponse,
  CommandResultResponse,
  FlatCommandRequest,
  LinkedAccountResponse,
  LinkRequest,
  PreventCommandRequest,
  SwitchAccountRequest,
)
from broker.schemas.trade_schema import PageMeta, TradeListResponse, TradeResponse
from broker.security.ensure_api_key import ensure_api_key

log = get_logger(__name__)

NOT_LINKED_RESPONSE = {404: {"description": "No account linked to this Telegram user."}}


async def get_linked_account(
  telegram_user_id: int,
  account_repo: AccountRepository = Depends(get_account_repository),
) -> Account:
  """Resolve the *active* account for ``telegram_user_id`` or raise 404.

  A Telegram user may have several linked accounts; single-account endpoints
  (trades, flat, prevent, unlink, status) all act on whichever one is
  currently active — see ``AccountRepository.get_active_account``.
  """
  account = await account_repo.get_active_account(telegram_user_id)
  if account is None:
    raise HTTPException(
      status_code=status.HTTP_404_NOT_FOUND,
      detail="No account linked to this Telegram user",
    )
  return account


def _scope(account_id: str, *, strategy: str | None, symbol: str | None) -> str:
  parts = [
    f"strategy={strategy}" if strategy else None,
    f"symbol={symbol}" if symbol else None,
    f"account={account_id}",
  ]
  return ", ".join(p for p in parts if p)


def get_telegram_router() -> APIRouter:
  router = APIRouter(
    dependencies=[Depends(ensure_api_key)],
    tags=["telegram"],
  )

  @router.post(
    "/link",
    summary="Link a Telegram user to an account via its link token",
    response_model=LinkedAccountResponse,
    responses={**AUTH_RESPONSES, 404: {"description": "Invalid link token."}},
  )
  async def link_account(
    body: LinkRequest,
    account_repo: AccountRepository = Depends(get_account_repository),
  ) -> LinkedAccountResponse:
    account = await account_repo.link_telegram(body.token, body.telegram_user_id)
    if account is None:
      raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Invalid link token",
      )
    active = await account_repo.get_active_account(body.telegram_user_id)
    resp = LinkedAccountResponse.model_validate(account)
    resp.is_active = active is not None and active.id == account.id
    return resp

  @router.get(
    "/{telegram_user_id}",
    summary="Get the account currently active for a Telegram user",
    response_model=LinkedAccountResponse,
    responses={**AUTH_RESPONSES, **NOT_LINKED_RESPONSE},
  )
  async def get_account(
    account: Account = Depends(get_linked_account),
  ) -> LinkedAccountResponse:
    resp = LinkedAccountResponse.model_validate(account)
    resp.is_active = True
    return resp

  @router.get(
    "/{telegram_user_id}/accounts",
    summary="List every account linked to a Telegram user",
    response_model=list[LinkedAccountResponse],
    responses=AUTH_RESPONSES,
  )
  async def list_accounts(
    telegram_user_id: int,
    account_repo: AccountRepository = Depends(get_account_repository),
  ) -> list[LinkedAccountResponse]:
    accounts, active = await asyncio.gather(
      account_repo.list_by_telegram_user_id(telegram_user_id),
      account_repo.get_active_account(telegram_user_id),
    )
    result: list[LinkedAccountResponse] = []
    for a in accounts:
      resp = LinkedAccountResponse.model_validate(a)
      resp.is_active = active is not None and active.id == a.id
      result.append(resp)
    return result

  @router.post(
    "/{telegram_user_id}/active-account",
    summary="Switch which of a Telegram user's linked accounts is active",
    response_model=LinkedAccountResponse,
    responses={**AUTH_RESPONSES, 404: {"description": "Account not found or not linked to this Telegram user."}},
  )
  async def set_active_account(
    telegram_user_id: int,
    body: SwitchAccountRequest,
    account_repo: AccountRepository = Depends(get_account_repository),
  ) -> LinkedAccountResponse:
    account = await account_repo.set_active_account(telegram_user_id, body.account_id)
    if account is None:
      raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Account not found or not linked to this Telegram user",
      )
    resp = LinkedAccountResponse.model_validate(account)
    resp.is_active = True
    return resp

  @router.post(
    "/{telegram_user_id}/unlink",
    summary="Unlink a Telegram user from their account",
    responses={**AUTH_RESPONSES, **NOT_LINKED_RESPONSE},
  )
  async def unlink_account(
    telegram_user_id: int,
    account_repo: AccountRepository = Depends(get_account_repository),
  ) -> dict[str, str]:
    ok = await account_repo.unlink_telegram(telegram_user_id)
    if not ok:
      raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="No account linked to this Telegram user",
      )
    return {"status": "unlinked"}

  @router.get(
    "/{telegram_user_id}/trades",
    summary="List trades for the caller's linked account",
    response_model=TradeListResponse,
    responses={**AUTH_RESPONSES, **NOT_LINKED_RESPONSE},
  )
  async def list_trades(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    order: Literal["asc", "desc"] = Query("desc"),
    order_by: Literal["updatedAt", "createdAt", "status", "symbol"] = Query(
      "updatedAt"
    ),
    account: Account = Depends(get_linked_account),
    trades_repo: TradeRepository = Depends(get_trade_repository),
  ) -> TradeListResponse:
    trades, total = await asyncio.gather(
      trades_repo.list_by_account(
        account.account_id, limit=limit, offset=offset, order=order, order_by=order_by
      ),
      trades_repo.count_by_account(account.account_id),
    )
    return TradeListResponse(
      data=[TradeResponse.model_validate(t) for t in trades],
      page=PageMeta(
        total=total, limit=limit, offset=offset, order=order, order_by=order_by
      ),
    )

  @router.post(
    "/{telegram_user_id}/commands/flat",
    summary="Flat (close) positions for the caller's account",
    response_model=CommandResultResponse,
    responses={**AUTH_RESPONSES, **NOT_LINKED_RESPONSE},
  )
  async def flat(
    body: FlatCommandRequest,
    account: Account = Depends(get_linked_account),
    publisher: SignalPublisher = Depends(get_publisher),
  ) -> CommandResultResponse:
    await publisher.publish_admin_signal(
      action=AdminActionEnum.FLAT,
      timestamp=datetime.now(timezone.utc),
      strategy=body.strategy,
      symbol=body.symbol,
      account_id=account.account_id,
      market=account.market,
      gateway=account.gateway,
    )
    scope = _scope(account.account_id, strategy=body.strategy, symbol=body.symbol)
    log.info("Telegram FLAT published scope=%s", scope)
    return CommandResultResponse(action=AdminActionEnum.FLAT.value, scope=scope)

  @router.post(
    "/{telegram_user_id}/commands/prevent",
    summary="Block or allow new entries for the caller's account",
    response_model=CommandResultResponse,
    responses={**AUTH_RESPONSES, **NOT_LINKED_RESPONSE},
  )
  async def prevent(
    body: PreventCommandRequest,
    account: Account = Depends(get_linked_account),
    publisher: SignalPublisher = Depends(get_publisher),
  ) -> CommandResultResponse:
    action = (
      AdminActionEnum.BLOCK_SIGNAL if body.enabled else AdminActionEnum.ALLOW_SIGNAL
    )
    await publisher.publish_admin_signal(
      action=action,
      timestamp=datetime.now(timezone.utc),
      account_id=account.account_id,
      market=account.market,
      gateway=account.gateway,
    )
    scope = _scope(account.account_id, strategy=None, symbol=None)
    log.info("Telegram %s published scope=%s", action.value, scope)
    return CommandResultResponse(action=action.value, scope=scope)

  # ── Completed-trade broadcast opt-in ─────────────────────────────
  # A per-user preference (spans every account the user holds), so these are
  # keyed by the path ``telegram_user_id`` alone and need no linked-account
  # resolution — an owner may subscribe before or after linking.

  @router.get(
    "/{telegram_user_id}/broadcast",
    summary="Whether the caller receives completed-trade broadcast DMs",
    response_model=BroadcastSubscriptionResponse,
    responses=AUTH_RESPONSES,
  )
  async def get_broadcast_subscription(
    telegram_user_id: int,
    broadcast_repo: TradeBroadcastRepository = Depends(get_trade_broadcast_repository),
  ) -> BroadcastSubscriptionResponse:
    subscribed = await broadcast_repo.is_subscribed(telegram_user_id)
    return BroadcastSubscriptionResponse(subscribed=subscribed)

  @router.post(
    "/{telegram_user_id}/broadcast/subscribe",
    summary="Opt in to completed-trade broadcast DMs",
    response_model=BroadcastSubscriptionResponse,
    responses=AUTH_RESPONSES,
  )
  async def subscribe_broadcast(
    telegram_user_id: int,
    broadcast_repo: TradeBroadcastRepository = Depends(get_trade_broadcast_repository),
  ) -> BroadcastSubscriptionResponse:
    ok = await broadcast_repo.subscribe(telegram_user_id)
    if not ok:
      raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Failed to update broadcast subscription",
      )
    return BroadcastSubscriptionResponse(subscribed=True)

  @router.post(
    "/{telegram_user_id}/broadcast/unsubscribe",
    summary="Opt out of completed-trade broadcast DMs",
    response_model=BroadcastSubscriptionResponse,
    responses=AUTH_RESPONSES,
  )
  async def unsubscribe_broadcast(
    telegram_user_id: int,
    broadcast_repo: TradeBroadcastRepository = Depends(get_trade_broadcast_repository),
  ) -> BroadcastSubscriptionResponse:
    ok = await broadcast_repo.unsubscribe(telegram_user_id)
    if not ok:
      raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Failed to update broadcast subscription",
      )
    return BroadcastSubscriptionResponse(subscribed=False)

  return router
