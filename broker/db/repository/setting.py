"""
broker/db/repository/setting.py — the ``broker_settings`` key/value table
(``SqlAlchemySettingRepository``).
"""

from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from broker.db.models import BrokerSetting
from broker.db.repository import _session
from broker.logger import get_logger

log = get_logger(__name__)


class SqlAlchemySettingRepository:
  """Reads and upserts rows in the ``broker_settings`` table."""

  async def get(self, key: str) -> str | None:
    """Return the value for the given key, or None if not found."""
    try:
      async with _session.get_session() as session:
        result = await session.execute(
          select(BrokerSetting).where(BrokerSetting.key == key)
        )
        row: Optional[BrokerSetting] = result.scalars().first()
        return row.value if row is not None else None
    except Exception as exc:
      log.exception("Failed to read broker setting key=%s: %s", key, exc)
      return None

  async def get_strict(self, key: str) -> str | None:
    """Like :meth:`get`, but a database error raises instead of reading as
    "not set".

    For settings that gate something — ``SIGNAL_BLOCKED`` above all — where
    answering ``None`` on a failed read silently opens the gate. ``None``
    still means the row does not exist.
    """
    async with _session.get_session() as session:
      result = await session.execute(
        select(BrokerSetting).where(BrokerSetting.key == key)
      )
      row: Optional[BrokerSetting] = result.scalars().first()
      return row.value if row is not None else None

  async def get_many(self, keys: list[str]) -> dict[str, str]:
    """Return {key: value} for every *keys* found in a single round trip;
    keys with no row are omitted (callers distinguish "missing" via absence).

    A single query also makes the read atomic under Postgres MVCC — every key
    reflects the same snapshot, unlike issuing one `get()` per key.
    """
    try:
      async with _session.get_session() as session:
        result = await session.execute(
          select(BrokerSetting).where(BrokerSetting.key.in_(keys))
        )
        return {row.key: row.value for row in result.scalars().all()}
    except Exception as exc:
      log.exception("Failed to read broker settings keys=%s: %s", keys, exc)
      return {}

  async def get_many_strict(self, keys: list[str]) -> dict[str, str]:
    """Like :meth:`get_many`, but a database error raises instead of reading
    as "none of these keys exist".

    For reads whose answer is handed on and kept — the WORKER_CONNECTED
    handshake caches these and a worker runs on the ACK until it reconnects —
    where an empty result on a failed read would pass for "not configured".
    """
    async with _session.get_session() as session:
      result = await session.execute(
        select(BrokerSetting).where(BrokerSetting.key.in_(keys))
      )
      return {row.key: row.value for row in result.scalars().all()}

  async def set(self, key: str, value: str) -> bool:
    """Upsert a broker_settings row. Returns True on success, False on error.

    One statement, resolved by Postgres against the key's unique index, for the
    reason ``AccountRepository.update_settings`` spells out for its JSONB merge:
    a read-then-write loses to a concurrent writer. Two admin calls on the same
    key — the API and a bot command, say — both read "missing" and both insert,
    so one fails on the unique key and reports the setting as not applied; on an
    existing row the later write would simply drop the earlier one.
    """
    try:
      async with _session.get_session() as session:
        statement = pg_insert(BrokerSetting).values(
          id=uuid.uuid4(), key=key, value=value
        )
        await session.execute(
          statement.on_conflict_do_update(
            index_elements=[BrokerSetting.key],
            # ``updatedAt`` is set explicitly: the column's ``onupdate`` is an
            # ORM hook on a flushed instance, and nothing is flushed here —
            # Postgres resolves the conflict itself, so without this the
            # timestamp would stop moving the day this became an upsert.
            set_={"value": statement.excluded.value, "updatedAt": func.now()},
          )
        )
      log.debug("broker setting upserted key=%s value=%s", key, value)
      return True
    except Exception as exc:
      log.exception("Failed to upsert broker setting key=%s: %s", key, exc)
      return False
