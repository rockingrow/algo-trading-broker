"""
broker/services/account_settings_service.py
───────────────────────────────────────────
Resolving the ``settings`` block a worker runs on, and pushing it to a worker
that is already connected.

Two scopes feed one block. ``accounts.settings`` (JSONB, per account) holds what
the account's owner set from the bot; the broker-wide ``broker_settings`` rows
hold the default an account inherits when it set nothing of its own. A worker
must not have to know that split exists — it receives one complete
:class:`AccountSettings` in which every value is already a concrete one — so the
resolution lives here, in the single place both paths that hand settings to a
worker go through:

* the WORKER_CONNECTED handshake (``SystemEventConsumer._build_account_settings``),
  which is the durable one: whatever a worker is told here is what it runs on
  until it reconnects;
* the live ``WORKER_SETTINGS_UPDATE`` push, so an owner changing a value from the
  bot does not have to wait for the worker to reconnect.

Both call the same :func:`resolve_account_settings`, so the live push and the
next handshake can never disagree about what the account's cap is.

Every read here is best-effort by design: a missing or hand-corrupted setting
falls back to the documented default rather than raising, because the
alternative is a worker that gets no configuration at all. The push is
best-effort for a different reason — the database is the source of truth and the
next handshake re-delivers the value, so a NATS failure must not fail the bot
command that caused it.
"""

from __future__ import annotations

from typing import Optional

from pydantic import ValidationError

from broker.constants import DEFAULT_MAX_OPEN_ORDERS, MAX_OPEN_ORDERS_KEY
from broker.interfaces import AccountRepository, SettingRepository, SignalPublisher
from broker.logger import get_logger
from broker.schemas.account_schema import (
  AccountSettings,
  MarketTypeEnum,
  compose_worker_id,
)
from broker.schemas.publisher_schema import SystemActionEnum

log = get_logger(__name__)


def parse_global_max_open_orders(raw: Optional[str]) -> int:
  """Parse the broker-wide ``max_open_orders`` setting into a usable cap.

  Unset, non-numeric or negative all mean "nobody has configured this" and give
  :data:`~broker.constants.DEFAULT_MAX_OPEN_ORDERS`. ``0`` is *not* in that
  group: it is the documented way to disable the cap, so it is returned as-is.
  """
  if raw is None:
    return DEFAULT_MAX_OPEN_ORDERS
  try:
    value = int(raw)
  except (TypeError, ValueError):
    log.warning(
      "%s is not an integer: %r — using default %d",
      MAX_OPEN_ORDERS_KEY,
      raw,
      DEFAULT_MAX_OPEN_ORDERS,
    )
    return DEFAULT_MAX_OPEN_ORDERS
  if value < 0:
    log.warning(
      "%s must not be negative, got %r — using default %d",
      MAX_OPEN_ORDERS_KEY,
      raw,
      DEFAULT_MAX_OPEN_ORDERS,
    )
    return DEFAULT_MAX_OPEN_ORDERS
  return value


def resolve_account_settings(
  settings: AccountSettings, *, global_max_open_orders: int
) -> AccountSettings:
  """Fill the values *settings* leaves unset from the broker-wide defaults.

  Returns a copy — the caller's object (and the row it was parsed from) keeps
  its ``None``, which is what makes a later change to the global default apply
  to this account too. Only an account that explicitly set its own value is
  pinned to it.
  """
  if settings.max_open_orders is None:
    return settings.model_copy(update={"max_open_orders": global_max_open_orders})
  return settings


def parse_account_settings(raw: object, account_id: str) -> AccountSettings:
  """Shape an ``accounts.settings`` blob into the worker-facing settings block.

  Anything unusable — a row that predates the column, a hand-edited value of
  the wrong type, a key whose value doesn't fit its field — falls back to the
  schema defaults rather than raising: a bad blob must not cost the worker its
  whole configuration, and "no settings" is exactly what the defaults mean.
  Unknown keys are dropped by the model itself (``extra="ignore"``); they stay
  in the row, since writes merge rather than replace.

  The result is *unresolved*: ``max_open_orders`` is still ``None`` for an
  account that set none of its own. Pass it through
  :func:`resolve_account_settings` before it reaches a worker.
  """
  if not isinstance(raw, dict):
    if raw is not None:
      log.warning(
        "accounts.settings for account_id=%s is not an object, got %s — using defaults",
        account_id,
        type(raw).__name__,
      )
    return AccountSettings()
  try:
    return AccountSettings(**raw)
  except ValidationError as exc:
    log.error(
      "accounts.settings for account_id=%s is invalid: %s | raw=%r — using defaults",
      account_id,
      exc,
      raw,
    )
    return AccountSettings()


async def load_resolved_account_settings(
  *,
  account_repo: AccountRepository,
  setting_repo: SettingRepository,
  account_id: str,
  market: MarketTypeEnum,
  gateway: str,
) -> AccountSettings:
  """Read the account's row and the global defaults, and resolve them into the
  block a worker runs on.

  Used by the live push, where there is no handshake to piggyback on. A failed
  read of either scope falls back to the defaults and logs: pushing the default
  cap is wrong by at most one setting, while raising would turn a bot command
  the owner already saw succeed into a 500.
  """
  try:
    raw = await account_repo.get_settings(
      account_id=account_id, market=market, gateway=gateway
    )
  except Exception as exc:
    log.exception(
      "accounts.settings lookup failed account_id=%s: %s — using defaults",
      account_id,
      exc,
    )
    raw = None

  settings = parse_account_settings(raw, account_id)

  try:
    global_raw = await setting_repo.get(MAX_OPEN_ORDERS_KEY)
  except Exception as exc:
    log.exception(
      "%s lookup failed account_id=%s: %s — using default %d",
      MAX_OPEN_ORDERS_KEY,
      account_id,
      exc,
      DEFAULT_MAX_OPEN_ORDERS,
    )
    global_raw = None

  return resolve_account_settings(
    settings, global_max_open_orders=parse_global_max_open_orders(global_raw)
  )


async def push_worker_settings_update(
  publisher: SignalPublisher,
  *,
  account_id: str,
  market: MarketTypeEnum | str,
  gateway: Optional[str],
  settings: AccountSettings,
) -> None:
  """Push *settings* to the worker serving this account, if we can address it.

  A worker is addressed as ``<market>-<gateway>-<account_id>``, so an account
  whose ``gateway`` is still NULL (no handshake and no trade yet) cannot be
  reached and is skipped — it will read the value from its first handshake
  anyway, which is also what fills the gateway in.

  Best-effort throughout: the settings are already committed, so a publish that
  fails is logged and the caller's command still succeeds.
  """
  if not gateway:
    log.info(
      "WORKER_SETTINGS_UPDATE push skipped for account_id=%s: gateway not set yet",
      account_id,
    )
    return

  worker_id = compose_worker_id(market, gateway, account_id)
  try:
    await publisher.publish_system_settings_update(
      action=SystemActionEnum.WORKER_SETTINGS_UPDATE,
      account_id=worker_id,
      settings=settings,
    )
  except Exception as exc:
    log.exception("Failed to push WORKER_SETTINGS_UPDATE to %s: %s", worker_id, exc)
