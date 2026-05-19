"""
broker/db/engine.py
────────────────────
Async SQLAlchemy engine + session factory.
Runs Alembic migrations on startup via a thread pool (Alembic uses asyncio.run
internally, which requires a fresh event loop in a separate thread).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import (
  AsyncEngine,
  AsyncSession,
  async_sessionmaker,
  create_async_engine,
)

from broker.settings import settings
from broker.logger import get_logger

log = get_logger(__name__)

_ALEMBIC_INI = Path(__file__).parent.parent.parent / "alembic.ini"

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _run_alembic_upgrade() -> None:
  cfg = Config(str(_ALEMBIC_INI))
  command.upgrade(cfg, "head")


async def init_db() -> None:
  """Create the async engine, session factory, and run Alembic migrations."""
  global _engine, _session_factory

  log.info(
    "Connecting to PostgreSQL: %s:%d/%s",
    settings.POSTGRES_HOST,
    settings.POSTGRES_PORT,
    settings.POSTGRES_DB,
  )

  _engine = create_async_engine(
    settings.postgres_dsn,
    echo=False,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
  )

  _session_factory = async_sessionmaker(
    bind=_engine,
    class_=AsyncSession,
    expire_on_commit=False,
  )

  # Alembic calls asyncio.run() inside env.py, which requires a thread with no
  # running event loop — asyncio.to_thread provides exactly that.
  await asyncio.to_thread(_run_alembic_upgrade)

  log.info("PostgreSQL migrations complete.")


async def close_db() -> None:
  """Dispose engine on shutdown."""
  global _engine
  if _engine:
    await _engine.dispose()
    log.info("PostgreSQL engine disposed.")


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
  """Async context manager that yields a session and commits/rolls back."""
  if _session_factory is None:
    raise RuntimeError("Database not initialised — call init_db() first.")
  async with _session_factory() as session:
    try:
      yield session
      await session.commit()
    except Exception:
      await session.rollback()
      raise
