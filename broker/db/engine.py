"""
broker/db/engine.py
────────────────────
Async SQLAlchemy engine + session factory.
Call `init_db()` once at startup to create tables.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
  AsyncEngine,
  AsyncSession,
  async_sessionmaker,
  create_async_engine,
)

from broker.db.models import Base
from broker.settings import settings
from broker.logger import get_logger

log = get_logger(__name__)

# Module-level singletons (initialised in init_db)
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def init_db() -> None:
  """Create the async engine, session factory, and all DB tables."""
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

  async with _engine.begin() as conn:
    await conn.run_sync(Base.metadata.create_all)

  log.info("PostgreSQL tables initialised.")


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
