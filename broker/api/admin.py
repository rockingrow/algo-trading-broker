import uuid as uuid_lib
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status

from broker.constants import (
  CRYPTO_ALLOWED_SYMBOL_KEY,
  CRYPTO_MAX_LEVERAGE_KEY,
  NOTIFICATION_INCLUDE_SIGNAL_RAW,
  NOTIFICATION_TIMEZONE_KEY,
  SIGNAL_BLOCKED,
  SILENT_SIGNAL,
)
from broker.helpers import emoji_constants as em
from broker.helpers.timezone_helper import (
  format_offset_value,
  format_utc_label,
  parse_offset_hours,
)
from broker.providers import (
  get_account_repository,
  get_admin_notifier,
  get_publisher,
  get_setting_repository,
)
from broker.interfaces import (
  AccountRepository,
  Notifier,
  SettingRepository,
  SignalPublisher,
)
from broker.schemas.account_schema import (
  AccountResponse,
  GATEWAYS_BY_MARKET,
  MarketTypeEnum,
  compose_worker_id,
)
from broker.schemas.admin_schema import (
  AdminLinkTelegramRequest,
  AdminResponse,
  CreateAccountRequest,
  CryptoAllowedSymbolRequest,
  CryptoMaxLeverageRequest,
  NotificationTimezoneRequest,
  RotateTokenResponse,
  SettingToggleResponse,
  SettingValueResponse,
  FlatRequest,
)
from broker.schemas.publisher_schema import (
  AdminActionEnum,
  SystemActionEnum,
)
from broker.logger import get_logger
from broker.openapi import AUTH_RESPONSES
from broker.security.ensure_api_key import ensure_api_key

log = get_logger(__name__)


async def _push_crypto_leverage_init(
  publisher: SignalPublisher,
  setting_repo: SettingRepository,
  account_repo: AccountRepository,
) -> None:
  """Push the current crypto config to each known crypto worker.

  After an admin changes ``crypto_allowed_symbol`` or ``crypto_max_leverage``,
  send a ``CRYPTO_LEVERAGE_INIT`` on the shared SYSTEM subject addressed to every
  crypto account by its ``<market>-<gateway>-<account_id>`` worker id, so workers
  apply the new configuration right away instead of waiting for their next
  ``WORKER_CONNECTED`` handshake (up to ``CRYPTO_SETTINGS_CACHE_TTL_SECONDS``).

  Both settings are read back from the DB in one ``get_many`` — the caller has
  already persisted its own change, so the payload always reflects the committed
  settings and stays atomic. Mirrors ``SystemEventConsumer``'s validation, but
  best-effort: the setting is the source of truth and reaches workers on their
  next handshake regardless, so a payload we cannot build (the complementary
  setting is missing or invalid) or a publish that fails is logged, never
  surfaced to the admin caller.
  """
  values = await setting_repo.get_many(
    [CRYPTO_ALLOWED_SYMBOL_KEY, CRYPTO_MAX_LEVERAGE_KEY]
  )
  symbols_raw = values.get(CRYPTO_ALLOWED_SYMBOL_KEY)
  leverage_raw = values.get(CRYPTO_MAX_LEVERAGE_KEY)

  if symbols_raw is None or leverage_raw is None:
    log.warning(
      "CRYPTO_LEVERAGE_INIT push skipped: missing settings (%s=%r, %s=%r)",
      CRYPTO_ALLOWED_SYMBOL_KEY,
      symbols_raw,
      CRYPTO_MAX_LEVERAGE_KEY,
      leverage_raw,
    )
    return

  symbols = [s.strip() for s in symbols_raw.split(",") if s.strip()]
  try:
    default_leverage = int(leverage_raw)
  except ValueError:
    log.error(
      "CRYPTO_LEVERAGE_INIT push skipped: %s is not an integer: %r",
      CRYPTO_MAX_LEVERAGE_KEY,
      leverage_raw,
    )
    return

  if default_leverage <= 0:
    log.error(
      "CRYPTO_LEVERAGE_INIT push skipped: %s must be positive, got %r",
      CRYPTO_MAX_LEVERAGE_KEY,
      leverage_raw,
    )
    return

  accounts = await account_repo.get_by_market(MarketTypeEnum.CRYPTO)
  if not accounts:
    log.info("CRYPTO_LEVERAGE_INIT push: no crypto accounts to notify")
    return

  for account in accounts:
    if not account.gateway:
      log.warning(
        "CRYPTO_LEVERAGE_INIT push skipped for account_id=%s: gateway not set",
        account.account_id,
      )
      continue

    worker_id = compose_worker_id(
      account.market, account.gateway, account.account_id
    )
    try:
      await publisher.publish_system_signal(
        action=SystemActionEnum.CRYPTO_LEVERAGE_INIT,
        account_id=worker_id,
        symbols=symbols,
        default_leverage=default_leverage,
      )
    except Exception as exc:
      log.exception("Failed to push CRYPTO_LEVERAGE_INIT to %s: %s", worker_id, exc)


def get_admin_router() -> APIRouter:
  router = APIRouter(dependencies=[Depends(ensure_api_key)])

  @router.get(
    "/settings",
    tags=["settings"],
    summary="Read broker toggle settings",
    response_model=list[SettingToggleResponse],
    responses=AUTH_RESPONSES,
  )
  async def get_settings(
    setting_repo: SettingRepository = Depends(get_setting_repository),
  ) -> list[SettingToggleResponse]:
    """Return the current state of the runtime broker toggles (unset = '0')."""
    keys = (SIGNAL_BLOCKED, SILENT_SIGNAL, NOTIFICATION_INCLUDE_SIGNAL_RAW)
    results: list[SettingToggleResponse] = []
    for key in keys:
      value = await setting_repo.get(key) or "0"
      state_label = "ENABLED" if value == "1" else "DISABLED"
      results.append(SettingToggleResponse(setting=key, value=value, state=state_label))
    return results

  @router.post(
    "/settings/block-signal",
    tags=["settings"],
    summary="Toggle signal blocking",
    responses={
      **AUTH_RESPONSES,
      500: {"description": "Failed to persist the setting."},
    },
  )
  async def toggle_block_signal(
    setting_repo: SettingRepository = Depends(get_setting_repository),
    notifier: Notifier = Depends(get_admin_notifier),
  ) -> SettingToggleResponse:
    """Toggle SIGNAL_BLOCKED between '1' (enabled) and '0' (disabled)."""
    current = await setting_repo.get(SIGNAL_BLOCKED)
    new_value = "0" if current == "1" else "1"

    ok = await setting_repo.set(SIGNAL_BLOCKED, new_value)
    if not ok:
      raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Failed to update broker setting",
      )

    state_label = "ENABLED" if new_value == "1" else "DISABLED"
    log.info("SIGNAL_BLOCKED toggled: %s -> %s", current, new_value)

    await notifier.send_message(
      f"{em.GEAR} <b>Broker setting changed</b>\n"
      f"Setting: <code>{SIGNAL_BLOCKED}</code>\n"
      f"Signal blocked: <b>{state_label}</b>\n"
    )

    return SettingToggleResponse(
      setting=SIGNAL_BLOCKED, value=new_value, state=state_label
    )

  @router.post(
    "/settings/silent-signal",
    tags=["settings"],
    summary="Toggle silent signal",
    responses={
      **AUTH_RESPONSES,
      500: {"description": "Failed to persist the setting."},
    },
  )
  async def toggle_silent_signal(
    setting_repo: SettingRepository = Depends(get_setting_repository),
    notifier: Notifier = Depends(get_admin_notifier),
  ) -> SettingToggleResponse:
    """Toggle SILENT_SIGNAL between '1' (enabled) and '0' (disabled)."""
    current = await setting_repo.get(SILENT_SIGNAL)
    new_value = "0" if current == "1" else "1"

    ok = await setting_repo.set(SILENT_SIGNAL, new_value)
    if not ok:
      raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Failed to update broker setting",
      )

    state_label = "ENABLED" if new_value == "1" else "DISABLED"
    log.info("SILENT_SIGNAL toggled: %s -> %s", current, new_value)

    await notifier.send_message(
      f"{em.GEAR} <b>Broker setting changed</b>\n"
      f"Setting: <code>{SILENT_SIGNAL}</code>\n"
      f"Silent signal: <b>{state_label}</b>\n"
    )

    return SettingToggleResponse(
      setting=SILENT_SIGNAL, value=new_value, state=state_label
    )

  @router.post(
    "/settings/include-signal-raw",
    tags=["settings"],
    summary="Toggle raw signal inclusion in notifications",
    responses={
      **AUTH_RESPONSES,
      500: {"description": "Failed to persist the setting."},
    },
  )
  async def toggle_include_signal_raw(
    setting_repo: SettingRepository = Depends(get_setting_repository),
    notifier: Notifier = Depends(get_admin_notifier),
  ) -> SettingToggleResponse:
    """Toggle NOTIFICATION_INCLUDE_SIGNAL_RAW between '1' (enabled) and '0' (disabled)."""
    current = await setting_repo.get(NOTIFICATION_INCLUDE_SIGNAL_RAW)
    new_value = "0" if current == "1" else "1"

    ok = await setting_repo.set(NOTIFICATION_INCLUDE_SIGNAL_RAW, new_value)
    if not ok:
      raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Failed to update broker setting",
      )

    state_label = "ENABLED" if new_value == "1" else "DISABLED"
    log.info("NOTIFICATION_INCLUDE_SIGNAL_RAW toggled: %s -> %s", current, new_value)

    await notifier.send_message(
      f"{em.GEAR} <b>Broker setting changed</b>\n"
      f"Setting: <code>{NOTIFICATION_INCLUDE_SIGNAL_RAW}</code>\n"
      f"Include signal raw: <b>{state_label}</b>\n"
    )

    return SettingToggleResponse(
      setting=NOTIFICATION_INCLUDE_SIGNAL_RAW, value=new_value, state=state_label
    )

  @router.post(
    "/settings/crypto-allowed-symbol",
    tags=["settings"],
    summary="Set the crypto allowed-symbol list",
    responses={
      **AUTH_RESPONSES,
      500: {"description": "Failed to persist the setting."},
    },
  )
  async def set_crypto_allowed_symbol(
    body: CryptoAllowedSymbolRequest,
    setting_repo: SettingRepository = Depends(get_setting_repository),
    notifier: Notifier = Depends(get_admin_notifier),
    publisher: SignalPublisher = Depends(get_publisher),
    account_repo: AccountRepository = Depends(get_account_repository),
  ) -> SettingValueResponse:
    """Set CRYPTO_ALLOWED_SYMBOL_KEY and push SYSTEM CRYPTO_LEVERAGE_INIT to each
    crypto worker (they also pick it up on their next connect)."""
    symbols = list(dict.fromkeys(s.strip().upper() for s in body.symbols if s.strip()))
    if not symbols:
      raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail="symbols must contain at least one non-empty value",
      )
    value = ",".join(symbols)

    ok = await setting_repo.set(CRYPTO_ALLOWED_SYMBOL_KEY, value)
    if not ok:
      raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Failed to update broker setting",
      )

    log.info("%s updated -> %s", CRYPTO_ALLOWED_SYMBOL_KEY, value)
    await notifier.send_message(
      f"{em.GEAR} <b>Broker setting changed</b>\n"
      f"Setting: <code>{CRYPTO_ALLOWED_SYMBOL_KEY}</code>\n"
      f"Symbols: <b>{value}</b>\n"
    )

    await _push_crypto_leverage_init(publisher, setting_repo, account_repo)

    return SettingValueResponse(setting=CRYPTO_ALLOWED_SYMBOL_KEY, value=value)

  @router.post(
    "/settings/crypto-max-leverage",
    tags=["settings"],
    summary="Set the default crypto leverage",
    responses={
      **AUTH_RESPONSES,
      500: {"description": "Failed to persist the setting."},
    },
  )
  async def set_crypto_max_leverage(
    body: CryptoMaxLeverageRequest,
    setting_repo: SettingRepository = Depends(get_setting_repository),
    notifier: Notifier = Depends(get_admin_notifier),
    publisher: SignalPublisher = Depends(get_publisher),
    account_repo: AccountRepository = Depends(get_account_repository),
  ) -> SettingValueResponse:
    """Set CRYPTO_MAX_LEVERAGE_KEY and push SYSTEM CRYPTO_LEVERAGE_INIT to each
    crypto worker (they also pick it up on their next connect). Must be a
    positive integer (enforced by CryptoMaxLeverageRequest)."""
    value = str(body.default_leverage)

    ok = await setting_repo.set(CRYPTO_MAX_LEVERAGE_KEY, value)
    if not ok:
      raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Failed to update broker setting",
      )

    log.info("%s updated -> %s", CRYPTO_MAX_LEVERAGE_KEY, value)
    await notifier.send_message(
      f"{em.GEAR} <b>Broker setting changed</b>\n"
      f"Setting: <code>{CRYPTO_MAX_LEVERAGE_KEY}</code>\n"
      f"Default leverage: <b>{value}</b>\n"
    )

    await _push_crypto_leverage_init(publisher, setting_repo, account_repo)

    return SettingValueResponse(setting=CRYPTO_MAX_LEVERAGE_KEY, value=value)

  @router.get(
    "/settings/notification-timezone",
    tags=["settings"],
    summary="Get the notification display timezone",
    response_model=SettingValueResponse,
    responses=AUTH_RESPONSES,
  )
  async def get_notification_timezone(
    setting_repo: SettingRepository = Depends(get_setting_repository),
  ) -> SettingValueResponse:
    """Current NOTIFICATION_TIMEZONE_KEY UTC offset (in hours), falling back
    to UTC+7 when unset — the same default `format_notification_time` uses."""
    hours = parse_offset_hours(await setting_repo.get(NOTIFICATION_TIMEZONE_KEY))
    return SettingValueResponse(
      setting=NOTIFICATION_TIMEZONE_KEY, value=format_offset_value(hours)
    )

  @router.post(
    "/settings/notification-timezone",
    tags=["settings"],
    summary="Set the notification display timezone",
    responses={
      **AUTH_RESPONSES,
      500: {"description": "Failed to persist the setting."},
    },
  )
  async def set_notification_timezone(
    body: NotificationTimezoneRequest,
    setting_repo: SettingRepository = Depends(get_setting_repository),
    notifier: Notifier = Depends(get_admin_notifier),
  ) -> SettingValueResponse:
    """Set NOTIFICATION_TIMEZONE_KEY, the UTC offset (in hours) applied to the
    "Time:" line of Telegram notifications. Falls back to UTC+7 when unset."""
    value = format_offset_value(body.utc_offset_hours)

    ok = await setting_repo.set(NOTIFICATION_TIMEZONE_KEY, value)
    if not ok:
      raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Failed to update broker setting",
      )

    log.info("%s updated -> %s", NOTIFICATION_TIMEZONE_KEY, value)
    await notifier.send_message(
      f"{em.GEAR} <b>Broker setting changed</b>\n"
      f"Setting: <code>{NOTIFICATION_TIMEZONE_KEY}</code>\n"
      f"Notification timezone: <b>{format_utc_label(body.utc_offset_hours)}</b>\n"
    )

    return SettingValueResponse(setting=NOTIFICATION_TIMEZONE_KEY, value=value)

  @router.post(
    "/flat",
    tags=["trading"],
    summary="Flat positions",
    description=(
      "Publish a FLAT directive. Pass strategy and/or symbol to narrow scope. To scope to one "
      "account, account_id, market, and gateway are all REQUIRED together (422 if only "
      "account_id is given) — account_id alone no longer identifies a single account. Omit "
      "all three (strategy/symbol still allowed) to flat everything.\n\n"
      "KNOWN LIMITATION: broadcast on the shared ADMIN subject to every worker; each worker "
      "filters for itself client-side (worker code, outside this repo). Broker now always "
      "sends the full (account_id, market, gateway) triple when scoped, but a worker "
      "that still matches on account_id alone can act on a FLAT meant for a different "
      "account sharing that id — the worker side must be updated to check all three."
    ),
    responses={
      **AUTH_RESPONSES,
      422: {"description": "account_id given without market and gateway."},
    },
  )
  async def flat_positions(
    body: FlatRequest,
    publisher: SignalPublisher = Depends(get_publisher),
    notifier: Notifier = Depends(get_admin_notifier),
  ) -> AdminResponse:
    await publisher.publish_admin_signal(
      action=AdminActionEnum.FLAT,
      timestamp=datetime.now(timezone.utc),
      strategy=body.strategy,
      symbol=body.symbol,
      account_id=body.account_id,
      market=body.market,
      gateway=body.gateway,
    )

    scope_parts = [
      f"strategy={body.strategy}" if body.strategy else None,
      f"symbol={body.symbol}" if body.symbol else None,
      f"account={body.account_id}" if body.account_id else None,
      f"market={body.market.value}" if body.market else None,
      f"gateway={body.gateway}" if body.gateway else None,
    ]
    scope = ", ".join(p for p in scope_parts if p) or "ALL"
    log.info("FLAT published scope=%s", scope)

    await notifier.send_message(
      f"{em.ADMIN_FLAT} <b>[ADMIN]FLAT</b>\nScope: <code>{scope}</code>\n"
    )

    return AdminResponse(action="FLAT", scope=scope)

  @router.post(
    "/accounts",
    tags=["accounts"],
    summary="Manually register a trading account",
    description=(
      "Pre-register an account (market, gateway, account_id) before it "
      "has traded or its worker has connected, so an admin can hand a link "
      "token to the end-user right away."
    ),
    status_code=status.HTTP_201_CREATED,
    responses={
      **AUTH_RESPONSES,
      409: {"description": "account_id already exists."},
      422: {"description": "gateway is not valid for market."},
    },
  )
  async def create_account(
    body: CreateAccountRequest,
    account_repo: AccountRepository = Depends(get_account_repository),
  ) -> AccountResponse:
    valid_gateways = GATEWAYS_BY_MARKET.get(body.market, [])
    if body.gateway not in valid_gateways:
      raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=f"gateway must be one of {valid_gateways} for market={body.market.value}",
      )

    account = await account_repo.create_account(
      body.account_id, body.market, body.gateway, body.account_name
    )
    if account is None:
      raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="account_id already exists",
      )
    log.info("Account manually created account_id=%s", body.account_id)
    resp = AccountResponse.model_validate(account)
    # create_account also mints the account's first link token; surface it so
    # the admin can hand it out without a second round trip.
    summary = (await account_repo.get_link_summaries([account.id])).get(account.id)
    if summary is not None:
      resp.link_token = summary.link_token
      resp.linked_user_ids = summary.linked_user_ids
    return resp

  @router.post(
    "/accounts/{account_id}/link-token/rotate",
    tags=["accounts"],
    summary="Rotate an account's bot link token",
    description=(
      "Issue a fresh link token for the account, revoking every token that "
      "was still valid. Hand the new token to the end-user so they can link "
      "the bot. Already-linked users keep their access — a token only grants "
      "the initial claim."
    ),
    responses={
      **AUTH_RESPONSES,
      404: {"description": "Account not found."},
    },
  )
  async def rotate_link_token(
    account_id: str,
    account_repo: AccountRepository = Depends(get_account_repository),
  ) -> RotateTokenResponse:
    new_token = await account_repo.rotate_link_token(account_id)
    if new_token is None:
      raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Account not found",
      )
    log.info("Link token rotated for account_id=%s", account_id)
    return RotateTokenResponse(account_id=account_id, link_token=new_token)

  @router.post(
    "/accounts/{account_uuid}/link-telegram",
    tags=["accounts"],
    summary="Admin-link a Telegram user to an account",
    description=(
      "Bind a Telegram user to an account directly, without handing out a link "
      "token. The account is addressed by its row UUID (``accounts.id``) rather "
      "than the reusable bare ``account_id`` so the target is unambiguous. "
      "Additive and idempotent: re-linking the same user is a no-op, and other "
      "linked users are untouched."
    ),
    response_model=AccountResponse,
    responses={
      **AUTH_RESPONSES,
      404: {"description": "Account not found."},
    },
  )
  async def link_telegram_to_account(
    account_uuid: uuid_lib.UUID,
    body: AdminLinkTelegramRequest,
    account_repo: AccountRepository = Depends(get_account_repository),
  ) -> AccountResponse:
    account = await account_repo.admin_link_telegram(
      account_uuid, body.telegram_user_id
    )
    if account is None:
      raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Account not found",
      )
    log.info(
      "Admin linked telegram_user_id=%s to account uuid=%s",
      body.telegram_user_id,
      account_uuid,
    )
    resp = AccountResponse.model_validate(account)
    summary = (await account_repo.get_link_summaries([account.id])).get(account.id)
    if summary is not None:
      resp.link_token = summary.link_token
      resp.linked_user_ids = summary.linked_user_ids
    return resp

  return router
