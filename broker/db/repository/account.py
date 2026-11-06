"""
broker/db/repository/account.py — accounts, link tokens, bot links and sessions
(``SqlAlchemyAccountRepository``).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import cast, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB

from broker.db.models import (
  Account,
  AccountBotLink,
  AccountLinkToken,
  BotSession,
)
from broker.db.repository import _session
from broker.logger import get_logger
from broker.schemas.account_schema import AccountLinkSummary, MarketTypeEnum
from broker.schemas.core import BotPlatformTypeEnum

log = get_logger(__name__)


def _new_link_token(account_id: uuid.UUID) -> AccountLinkToken:
  """A fresh, never-expiring, unrevoked link token for *account_id*."""
  return AccountLinkToken(id=uuid.uuid4(), account_id=account_id, token=uuid.uuid4())


class SqlAlchemyAccountRepository:
  """Reads rows from the ``accounts`` table, and records the market/gateway a
  worker announces when it connects."""

  async def upsert_gateway(
    self, account_id: str, market: MarketTypeEnum, gateway: str
  ) -> None:
    """Persist the *market*/*gateway* a worker reported for *account_id*.

    Called on every ``WORKER_CONNECTED`` handshake. Without this, ``gateway`` is
    only ever written from a ``TRADE`` position event, so an account that has
    not traded since the column was introduced keeps a NULL gateway and cannot
    be addressed as ``<market>-<gateway>-<account_id>`` by the admin
    ``CRYPTO_LEVERAGE_INIT`` push.

    Inserts the row when the account is unknown (a worker that has connected but
    never traded is still addressable). Best-effort: failures are logged, never
    raised — the handshake reply matters more than this bookkeeping.

    Scoped by ``account_id`` + ``market`` (not ``account_id`` alone — that no
    longer identifies a single row, see ``uq_accounts_market_gateway_account_id``),
    matching either the exact gateway already on file or a legacy row whose
    gateway is still NULL (predates this column, being backfilled now).
    """
    try:
      async with _session.get_session() as session:
        result = await session.execute(
          select(Account).where(
            Account.account_id == account_id,
            Account.market == market,
            or_(Account.gateway == gateway, Account.gateway.is_(None)),
          )
        )
        row: Optional[Account] = result.scalars().first()

        if row is not None:
          if row.market == market and row.gateway == gateway:
            return
          row.market = market
          row.gateway = gateway
        else:
          new_account = Account(
            id=uuid.uuid4(),
            account_id=account_id,
            market=market,
            gateway=gateway,
            last_activity_at=datetime.now(timezone.utc),
          )
          session.add(new_account)
          session.add(_new_link_token(new_account.id))

      log.debug(
        "account gateway upserted account_id=%s market=%s gateway=%s",
        account_id,
        market.value,
        gateway,
      )
    except Exception as exc:
      log.exception(
        "Failed to upsert gateway for account_id=%s: %s",
        account_id,
        exc,
      )

  async def create_account(
    self,
    account_id: str,
    market: MarketTypeEnum,
    gateway: str,
    account_name: Optional[str] = None,
  ) -> Optional[Account]:
    """Manually register a new account (admin-initiated, ahead of any
    trade/handshake). Returns None if the (market, gateway, account_id) triple
    is already taken — the same bare account_id may exist under a different
    market/gateway (see ``uq_accounts_market_gateway_account_id``).
    """
    try:
      async with _session.get_session() as session:
        result = await session.execute(
          select(Account).where(
            Account.account_id == account_id,
            Account.market == market,
            Account.gateway == gateway,
          )
        )
        if result.scalars().first() is not None:
          return None

        account = Account(
          id=uuid.uuid4(),
          account_id=account_id,
          account_name=account_name,
          market=market,
          gateway=gateway,
          last_activity_at=datetime.now(timezone.utc),
        )
        session.add(account)
        # Every account needs a claimable token from the moment it exists —
        # this used to be the ``telegram_link_token`` column default.
        session.add(_new_link_token(account.id))
        await session.flush()
        await session.refresh(account)
        log.info(
          "Account created account_id=%s market=%s gateway=%s",
          account_id,
          market.value if isinstance(market, MarketTypeEnum) else market,
          gateway,
        )
        return account
    except Exception as exc:
      log.exception("Failed to create account_id=%s: %s", account_id, exc)
      return None

  async def get_all(self) -> list[Account]:
    """Return all accounts ordered by last_activity_at desc."""
    try:
      async with _session.get_session() as session:
        result = await session.execute(
          select(Account).order_by(Account.last_activity_at.desc())
        )
        return list(result.scalars().all())
    except Exception as exc:
      log.exception("Failed to fetch accounts: %s", exc)
      return []

  async def get_link_summaries(
    self,
    account_ids: list[uuid.UUID],
    platform: BotPlatformTypeEnum = BotPlatformTypeEnum.TELEGRAM,
  ) -> dict[uuid.UUID, AccountLinkSummary]:
    """Bulk-load the current link token and linked bot users for *account_ids*.

    Two queries regardless of how many accounts are asked for — the admin
    account list would otherwise be N+1 now that both facts live in their own
    tables. Accounts with neither a token nor a link are absent from the
    result; callers should treat a missing key as an empty summary.

    An account can hold several unrevoked tokens; ``link_token`` reports the
    most recently issued one, which is what ``rotate_link_token`` just handed
    the admin.
    """
    summaries: dict[uuid.UUID, AccountLinkSummary] = {}
    if not account_ids:
      return summaries

    now = datetime.now(timezone.utc)
    try:
      async with _session.get_session() as session:
        result = await session.execute(
          select(AccountLinkToken)
          .where(
            AccountLinkToken.account_id.in_(account_ids),
            AccountLinkToken.revoked_at.is_(None),
            or_(
              AccountLinkToken.expires_at.is_(None),
              AccountLinkToken.expires_at > now,
            ),
          )
          .order_by(AccountLinkToken.createdAt.asc())
        )
        for row in result.scalars().all():
          # asc order + overwrite => the newest token wins.
          summaries.setdefault(
            row.account_id, AccountLinkSummary()
          ).link_token = row.token

        result = await session.execute(
          select(AccountBotLink)
          .where(
            AccountBotLink.account_id.in_(account_ids),
            AccountBotLink.platform == platform,
          )
          .order_by(AccountBotLink.createdAt.asc())
        )
        for row in result.scalars().all():
          summaries.setdefault(
            row.account_id, AccountLinkSummary()
          ).linked_user_ids.append(row.platform_user_id)
    except Exception as exc:
      log.exception("Failed to fetch link summaries: %s", exc)
      return {}

    return summaries

  async def get_by_market(self, market: MarketTypeEnum) -> list[Account]:
    """Return accounts in *market* ordered by last_activity_at desc."""
    try:
      async with _session.get_session() as session:
        result = await session.execute(
          select(Account)
          .where(Account.market == market)
          .order_by(Account.last_activity_at.desc())
        )
        return list(result.scalars().all())
    except Exception as exc:
      log.exception("Failed to fetch accounts for market=%s: %s", market, exc)
      return []

  async def list_by_telegram_user_id(
    self,
    telegram_user_id: int,
    platform: BotPlatformTypeEnum = BotPlatformTypeEnum.TELEGRAM,
  ) -> list[Account]:
    """Return every account linked to a bot user (possibly several — see
    ``AccountBotLink``, and ``BotSession`` for which one is currently active),
    ordered ``last_activity_at`` desc with ``createdAt`` asc as a
    deterministic tie-break (two admin-created accounts that haven't traded
    yet both have ``last_activity_at IS NULL``).

    That ordering is a contract, not a detail: ``get_active_account`` and
    ``unlink_telegram`` both fall back to "the first row of this list."
    """
    platform_user_id = str(telegram_user_id)
    try:
      async with _session.get_session() as session:
        result = await session.execute(
          select(Account)
          .join(AccountBotLink, AccountBotLink.account_id == Account.id)
          .where(
            AccountBotLink.platform == platform,
            AccountBotLink.platform_user_id == platform_user_id,
          )
          .order_by(Account.last_activity_at.desc(), Account.createdAt.asc())
        )
        return list(result.scalars().all())
    except Exception as exc:
      log.exception(
        "Failed to fetch accounts for telegram_user_id=%s: %s",
        telegram_user_id,
        exc,
      )
      return []

  async def get_active_account(
    self,
    telegram_user_id: int,
    platform: BotPlatformTypeEnum = BotPlatformTypeEnum.TELEGRAM,
  ) -> Account | None:
    """Return the bot user's currently active account, or None if they have
    no linked accounts at all.

    Reads ``BotSession.active_account_id`` and validates the user still holds
    a link to it in the same query (covers a stale pointer left over from
    ``unlink_telegram``). Falls back to the first row from
    ``list_by_telegram_user_id`` when the session is missing, its pointer is
    NULL, or stale — self-healing the session best-effort so the next call
    hits the fast path (failure to self-heal is logged and non-fatal; the
    correct fallback account is still returned).
    """
    platform_user_id = str(telegram_user_id)
    try:
      async with _session.get_session() as session:
        result = await session.execute(
          select(BotSession).where(
            BotSession.platform == platform,
            BotSession.platform_user_id == platform_user_id,
          )
        )
        bot_session: Optional[BotSession] = result.scalars().first()

        if bot_session is not None and bot_session.active_account_id is not None:
          result = await session.execute(
            select(Account)
            .join(AccountBotLink, AccountBotLink.account_id == Account.id)
            .where(
              Account.id == bot_session.active_account_id,
              AccountBotLink.platform == platform,
              AccountBotLink.platform_user_id == platform_user_id,
            )
          )
          account: Optional[Account] = result.scalars().first()
          if account is not None:
            return account
    except Exception as exc:
      log.exception(
        "Failed to fetch active account for telegram_user_id=%s: %s",
        telegram_user_id,
        exc,
      )
      return None

    accounts = await self.list_by_telegram_user_id(telegram_user_id, platform)
    if not accounts:
      return None
    fallback = accounts[0]

    try:
      async with _session.get_session() as session:
        result = await session.execute(
          select(BotSession).where(
            BotSession.platform == platform,
            BotSession.platform_user_id == platform_user_id,
          )
        )
        bot_session = result.scalars().first()
        if bot_session is None:
          session.add(
            BotSession(
              id=uuid.uuid4(),
              platform=platform,
              platform_user_id=platform_user_id,
              active_account_id=fallback.id,
            )
          )
        else:
          bot_session.active_account_id = fallback.id
    except Exception as exc:
      log.warning(
        "Failed to self-heal active session for telegram_user_id=%s: %s",
        telegram_user_id,
        exc,
      )

    return fallback

  async def set_active_account(
    self,
    telegram_user_id: int,
    account_id: uuid.UUID,
    platform: BotPlatformTypeEnum = BotPlatformTypeEnum.TELEGRAM,
  ) -> Account | None:
    """Set the bot user's active account. Returns the account, or None if no
    account with that id is linked to this user.

    The ownership check (joining ``account_bot_links`` rather than looking the
    account up by id alone) is deliberate: unlike the rest of this class,
    which treats ``telegram_user_id`` as a trusted, bot-verified identity,
    ``account_id`` here is client-supplied and must be checked against it —
    a "not found" and a "found but not yours" both return None so no
    information about other users' account ids leaks.
    """
    platform_user_id = str(telegram_user_id)
    try:
      async with _session.get_session() as session:
        result = await session.execute(
          select(Account)
          .join(AccountBotLink, AccountBotLink.account_id == Account.id)
          .where(
            Account.id == account_id,
            AccountBotLink.platform == platform,
            AccountBotLink.platform_user_id == platform_user_id,
          )
        )
        account: Optional[Account] = result.scalars().first()
        if account is None:
          return None

        result = await session.execute(
          select(BotSession).where(
            BotSession.platform == platform,
            BotSession.platform_user_id == platform_user_id,
          )
        )
        bot_session: Optional[BotSession] = result.scalars().first()
        if bot_session is None:
          session.add(
            BotSession(
              id=uuid.uuid4(),
              platform=platform,
              platform_user_id=platform_user_id,
              active_account_id=account.id,
            )
          )
        else:
          bot_session.active_account_id = account.id

        await session.flush()
        await session.refresh(account)
        log.info(
          "Active account set telegram_user_id=%s account_id=%s",
          telegram_user_id,
          account.account_id,
        )
        return account
    except Exception as exc:
      log.exception(
        "Failed to set active account for telegram_user_id=%s account_id=%s: %s",
        telegram_user_id,
        account_id,
        exc,
      )
      return None

  async def link_telegram(
    self,
    token: uuid.UUID,
    telegram_user_id: int,
    platform: BotPlatformTypeEnum = BotPlatformTypeEnum.TELEGRAM,
  ) -> Account | None:
    """Link ``telegram_user_id`` to the account the ``token`` belongs to.

    Adds a row to ``account_bot_links``; it never removes one. Several users
    may hold the same account and one user may hold several accounts —
    linking is purely additive in both directions.

    If the user's ``BotSession`` is missing or has no active account yet, the
    newly linked account becomes active (covers both "first ever link" and
    "relinking after a full unlink"). Otherwise an existing active selection
    is left untouched — adding a 2nd or 3rd account doesn't disturb which one
    is currently active.

    Returns the linked account, or None if the token doesn't exist, has been
    revoked, or has expired. Re-linking an account the user already holds is
    a no-op that still returns the account (idempotent — the unique
    constraint must not be allowed to raise).
    """
    platform_user_id = str(telegram_user_id)
    now = datetime.now(timezone.utc)
    try:
      async with _session.get_session() as session:
        result = await session.execute(
          select(AccountLinkToken).where(
            AccountLinkToken.token == token,
            AccountLinkToken.revoked_at.is_(None),
            or_(
              AccountLinkToken.expires_at.is_(None),
              AccountLinkToken.expires_at > now,
            ),
          )
        )
        link_token: Optional[AccountLinkToken] = result.scalars().first()
        if link_token is None:
          return None

        result = await session.execute(
          select(Account).where(Account.id == link_token.account_id)
        )
        account: Optional[Account] = result.scalars().first()
        if account is None:
          return None

        link_token.last_used_at = now

        result = await session.execute(
          select(AccountBotLink).where(
            AccountBotLink.account_id == account.id,
            AccountBotLink.platform == platform,
            AccountBotLink.platform_user_id == platform_user_id,
          )
        )
        if result.scalars().first() is None:
          session.add(
            AccountBotLink(
              id=uuid.uuid4(),
              account_id=account.id,
              platform=platform,
              platform_user_id=platform_user_id,
            )
          )

        result = await session.execute(
          select(BotSession).where(
            BotSession.platform == platform,
            BotSession.platform_user_id == platform_user_id,
          )
        )
        bot_session: Optional[BotSession] = result.scalars().first()
        if bot_session is None:
          session.add(
            BotSession(
              id=uuid.uuid4(),
              platform=platform,
              platform_user_id=platform_user_id,
              active_account_id=account.id,
            )
          )
        elif bot_session.active_account_id is None:
          bot_session.active_account_id = account.id

        await session.flush()
        await session.refresh(account)
        log.info(
          "Linked telegram_user_id=%s to account_id=%s",
          telegram_user_id,
          account.account_id,
        )
        return account
    except Exception as exc:
      log.exception(
        "Failed to link telegram_user_id=%s with token=%s: %s",
        telegram_user_id,
        token,
        exc,
      )
      return None

  async def admin_link_telegram(
    self,
    account_uuid: uuid.UUID,
    telegram_user_id: int,
    platform: BotPlatformTypeEnum = BotPlatformTypeEnum.TELEGRAM,
  ) -> Account | None:
    """Admin-link ``telegram_user_id`` directly to the account with row id
    ``account_uuid``, bypassing the invite-token flow.

    Unlike ``link_telegram`` (which resolves the account from a token an
    end-user typed), this is called by an admin who already knows exactly which
    account row to bind, so it keys off the unambiguous ``accounts.id`` UUID
    rather than the reusable bare ``account_id``. Otherwise the semantics match
    ``link_telegram``: additive (never removes a link), idempotent on the unique
    constraint, and it makes the account active for the user only when they have
    no active selection yet.

    Returns the account, or None if no account with that id exists.
    """
    platform_user_id = str(telegram_user_id)
    try:
      async with _session.get_session() as session:
        result = await session.execute(
          select(Account).where(Account.id == account_uuid)
        )
        account: Optional[Account] = result.scalars().first()
        if account is None:
          return None

        result = await session.execute(
          select(AccountBotLink).where(
            AccountBotLink.account_id == account.id,
            AccountBotLink.platform == platform,
            AccountBotLink.platform_user_id == platform_user_id,
          )
        )
        if result.scalars().first() is None:
          session.add(
            AccountBotLink(
              id=uuid.uuid4(),
              account_id=account.id,
              platform=platform,
              platform_user_id=platform_user_id,
            )
          )

        result = await session.execute(
          select(BotSession).where(
            BotSession.platform == platform,
            BotSession.platform_user_id == platform_user_id,
          )
        )
        bot_session: Optional[BotSession] = result.scalars().first()
        if bot_session is None:
          session.add(
            BotSession(
              id=uuid.uuid4(),
              platform=platform,
              platform_user_id=platform_user_id,
              active_account_id=account.id,
            )
          )
        elif bot_session.active_account_id is None:
          bot_session.active_account_id = account.id

        await session.flush()
        await session.refresh(account)
        log.info(
          "Admin-linked telegram_user_id=%s to account_id=%s (uuid=%s)",
          telegram_user_id,
          account.account_id,
          account.id,
        )
        return account
    except Exception as exc:
      log.exception(
        "Failed to admin-link telegram_user_id=%s to account uuid=%s: %s",
        telegram_user_id,
        account_uuid,
        exc,
      )
      return None

  async def unlink_telegram(
    self,
    telegram_user_id: int,
    platform: BotPlatformTypeEnum = BotPlatformTypeEnum.TELEGRAM,
  ) -> bool:
    """Drop this user's link to their *active* account, and re-point the
    active selection at another account they still hold (if any) or clear it.
    Returns True if a row changed.

    Only this user's own link row is deleted — other users linked to the same
    account keep theirs, and the account itself is untouched.

    Deliberately calls ``get_active_account`` (its own ``_session.get_session()``
    block) rather than duplicating the active-resolution + fallback-ordering
    logic here — an extra round trip on this low-frequency, user-initiated
    action, in exchange for one source of truth for "what's active."
    """
    active = await self.get_active_account(telegram_user_id, platform)
    if active is None:
      return False

    platform_user_id = str(telegram_user_id)
    try:
      async with _session.get_session() as session:
        await session.execute(
          delete(AccountBotLink).where(
            AccountBotLink.account_id == active.id,
            AccountBotLink.platform == platform,
            AccountBotLink.platform_user_id == platform_user_id,
          )
        )
        await session.flush()

        result = await session.execute(
          select(Account)
          .join(AccountBotLink, AccountBotLink.account_id == Account.id)
          .where(
            AccountBotLink.platform == platform,
            AccountBotLink.platform_user_id == platform_user_id,
            Account.id != active.id,
          )
          .order_by(Account.last_activity_at.desc(), Account.createdAt.asc())
        )
        remaining: Optional[Account] = result.scalars().first()

        result = await session.execute(
          select(BotSession).where(
            BotSession.platform == platform,
            BotSession.platform_user_id == platform_user_id,
          )
        )
        bot_session: Optional[BotSession] = result.scalars().first()
        if bot_session is not None:
          bot_session.active_account_id = (
            remaining.id if remaining is not None else None
          )

        log.info(
          "Unlinked telegram_user_id=%s from account_id=%s",
          telegram_user_id,
          active.account_id,
        )
        return True
    except Exception as exc:
      log.exception("Failed to unlink telegram_user_id=%s: %s", telegram_user_id, exc)
      return False

  async def rotate_link_token(self, account_id: str) -> uuid.UUID | None:
    """Issue a fresh link token for an account, revoking every token that was
    still valid. Returns the new token, or None if the account doesn't exist.

    Revoking is a state change (``revoked_at``), not a delete, so an issued
    secret's history survives rotation.

    Rotation is also a full reset of *who may drive the account*: every platform
    (Telegram) user currently linked to it is unlinked, and any active-session
    pointer aimed at it is cleared. A rotated token therefore hands the account
    to whoever the admin next gives the new secret to, with no leftover access
    from the previous holders — the account_bot_links for it are deleted, not
    just the token revoked.

    KNOWN LIMITATION: resolves by bare ``account_id`` alone, which is not
    guaranteed unique (see ``uq_accounts_market_gateway_account_id`` — the
    same id can exist under different market/gateway pairs). If it collides,
    this rotates whichever matching row Postgres returns first,
    non-deterministically. Not fixed here: doing so needs the admin-facing
    callers (bot ``/rotate``, ``POST /admin/accounts/{account_id}/...``) to
    also pass market + gateway, which is a wider UX change than this
    schema migration. Avoid reusing account_id across gateways until then.
    """
    now = datetime.now(timezone.utc)
    try:
      async with _session.get_session() as session:
        result = await session.execute(
          select(Account).where(Account.account_id == account_id)
        )
        account: Optional[Account] = result.scalars().first()
        if account is None:
          return None

        result = await session.execute(
          select(AccountLinkToken).where(
            AccountLinkToken.account_id == account.id,
            AccountLinkToken.revoked_at.is_(None),
          )
        )
        for stale in result.scalars().all():
          stale.revoked_at = now

        # Unlink every platform user bound to this account and clear any
        # active-session pointer aimed at it. Rotating is a reset of access,
        # not just a new invite secret — see the docstring.
        await session.execute(
          delete(AccountBotLink).where(AccountBotLink.account_id == account.id)
        )
        result = await session.execute(
          select(BotSession).where(BotSession.active_account_id == account.id)
        )
        for bot_session in result.scalars().all():
          bot_session.active_account_id = None

        new_token = uuid.uuid4()
        session.add(
          AccountLinkToken(
            id=uuid.uuid4(),
            account_id=account.id,
            token=new_token,
          )
        )
        log.info("Rotated link token for account_id=%s", account_id)
        return new_token
    except Exception as exc:
      log.exception(
        "Failed to rotate link token for account_id=%s: %s", account_id, exc
      )
      return None

  # ── accounts.settings (per-account command state) ──────────────────

  async def get_settings(
    self, account_id: str, market: MarketTypeEnum, gateway: str
  ) -> dict:
    """Return the ``settings`` blob of a worker's account row.

    Read on every ``WORKER_CONNECTED`` handshake to fill the ``settings`` block
    of the ACK. Scoped by ``account_id`` + ``market`` + ``gateway`` — the same
    (gateway or legacy NULL) match ``upsert_gateway`` uses, since that call runs
    first on the handshake and may just have inserted the row.

    Returns ``{}`` for an unknown account or a failed read: a worker gets the
    schema defaults rather than no configuration at all.
    """
    try:
      async with _session.get_session() as session:
        result = await session.execute(
          select(Account.settings).where(
            Account.account_id == account_id,
            Account.market == market,
            or_(Account.gateway == gateway, Account.gateway.is_(None)),
          )
        )
        raw = result.scalars().first()
        return raw if isinstance(raw, dict) else {}
    except Exception as exc:
      log.exception(
        "Failed to read settings for account_id=%s market=%s gateway=%s: %s",
        account_id,
        market,
        gateway,
        exc,
      )
      return {}

  async def update_settings(self, account_uuid: uuid.UUID, patch: dict) -> dict | None:
    """Merge *patch* into an account's ``settings`` and return the new blob.

    Keyed by the row's UUID, not the bare ``account_id``, because the caller
    (the Telegram command endpoints) has already resolved the exact account and
    a bare id is not unique across market/gateway pairs.

    The merge happens in Postgres (``settings || patch``), not in Python: two
    commands landing at the same time would otherwise read the same blob and
    the second write would drop the first one's key. Only the keys in *patch*
    are touched — anything else in the blob, including a key written by a newer
    broker version, is preserved.

    Returns ``None`` when the account doesn't exist or the write failed, so the
    caller can report the command as failed instead of silently losing the
    setting.
    """
    try:
      async with _session.get_session() as session:
        result = await session.execute(
          update(Account)
          .where(Account.id == account_uuid)
          .values(
            settings=func.coalesce(Account.settings, cast({}, JSONB)).op("||")(
              cast(patch, JSONB)
            )
          )
          .returning(Account.settings)
        )
        merged = result.scalars().first()
        if merged is None:
          log.warning("No account row to update settings for id=%s", account_uuid)
          return None
        log.info(
          "Account settings updated id=%s patch=%s result=%s",
          account_uuid,
          patch,
          merged,
        )
        return merged
    except Exception as exc:
      log.exception(
        "Failed to update settings for account id=%s: %s", account_uuid, exc
      )
      return None
