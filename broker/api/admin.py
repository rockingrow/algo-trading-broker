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
from broker.helpers.timezone_helper import format_offset_value, format_utc_label
from broker.providers import get_admin_notifier, get_publisher, get_setting_repository
from broker.interfaces import Notifier, SettingRepository, SignalPublisher
from broker.schemas.admin_schema import (
  AdminResponse,
  CryptoAllowedSymbolRequest,
  CryptoMaxLeverageRequest,
  NotificationTimezoneRequest,
  SettingToggleResponse,
  SettingValueResponse,
  FlatRequest,
)
from broker.schemas.publisher_schema import (
  SYSTEM_BROADCAST_ACCOUNT_ID,
  AdminActionEnum,
  SystemActionEnum,
)
from broker.logger import get_logger
from broker.openapi import AUTH_RESPONSES
from broker.security.ensure_api_key import ensure_api_key

log = get_logger(__name__)


async def _broadcast_crypto_leverage_init(
  publisher: SignalPublisher,
  setting_repo: SettingRepository,
) -> None:
  """Push the current crypto config to every connected crypto worker.

  After an admin changes ``crypto_allowed_symbol`` or ``crypto_max_leverage``,
  broadcast a ``CRYPTO_LEVERAGE_INIT`` on the shared SYSTEM subject with the
  wildcard ``SYSTEM_BROADCAST_ACCOUNT_ID`` so workers apply the new configuration
  right away instead of waiting for their next ``WORKER_CONNECTED`` handshake
  (up to ``CRYPTO_SETTINGS_CACHE_TTL_SECONDS``).

  Both values are read back from the DB in one ``get_many`` — the caller has
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
      "CRYPTO_LEVERAGE_INIT broadcast skipped: missing settings (%s=%r, %s=%r)",
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
      "CRYPTO_LEVERAGE_INIT broadcast skipped: %s is not an integer: %r",
      CRYPTO_MAX_LEVERAGE_KEY,
      leverage_raw,
    )
    return

  if default_leverage <= 0:
    log.error(
      "CRYPTO_LEVERAGE_INIT broadcast skipped: %s must be positive, got %r",
      CRYPTO_MAX_LEVERAGE_KEY,
      leverage_raw,
    )
    return

  try:
    await publisher.publish_system_signal(
      action=SystemActionEnum.CRYPTO_LEVERAGE_INIT,
      account_id=SYSTEM_BROADCAST_ACCOUNT_ID,
      symbols=symbols,
      default_leverage=default_leverage,
    )
  except Exception as exc:
    log.exception("Failed to broadcast CRYPTO_LEVERAGE_INIT: %s", exc)


def get_admin_router() -> APIRouter:
  router = APIRouter(dependencies=[Depends(ensure_api_key)])

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
  ) -> SettingValueResponse:
    """Set CRYPTO_ALLOWED_SYMBOL_KEY and broadcast SYSTEM CRYPTO_LEVERAGE_INIT to
    every connected crypto worker (they also pick it up on their next connect)."""
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

    await _broadcast_crypto_leverage_init(publisher, setting_repo)

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
  ) -> SettingValueResponse:
    """Set CRYPTO_MAX_LEVERAGE_KEY and broadcast SYSTEM CRYPTO_LEVERAGE_INIT to
    every connected crypto worker (they also pick it up on their next connect).
    Must be a positive integer (enforced by CryptoMaxLeverageRequest)."""
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

    await _broadcast_crypto_leverage_init(publisher, setting_repo)

    return SettingValueResponse(setting=CRYPTO_MAX_LEVERAGE_KEY, value=value)

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
      "Publish a FLAT directive. Pass strategy, symbol, and/or account_id in the request body "
      "to scope the flat. Omit all fields (or send an empty body) to flat everything."
    ),
    responses={**AUTH_RESPONSES},
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
    )

    scope_parts = [
      f"strategy={body.strategy}" if body.strategy else None,
      f"symbol={body.symbol}" if body.symbol else None,
      f"account={body.account_id}" if body.account_id else None,
    ]
    scope = ", ".join(p for p in scope_parts if p) or "ALL"
    log.info("FLAT published scope=%s", scope)

    await notifier.send_message(
      f"{em.ADMIN_FLAT} <b>[ADMIN]FLAT</b>\nScope: <code>{scope}</code>\n"
    )

    return AdminResponse(action="FLAT", scope=scope)

  return router
