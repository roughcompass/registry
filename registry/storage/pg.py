"""Async engine + session factory.

PgBouncer in transaction mode does not support named prepared statements
across connections. asyncpg uses prepared statements by default, so we
disable its cache here. Removing the `prepared_statement_cache_size=0`
arg silently breaks queries under PgBouncer transaction mode.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from registry.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the async SQLAlchemy engine bound to the configured database URL."""
    return create_async_engine(
        settings.database_url,
        connect_args={"prepared_statement_cache_size": 0},  # required for PgBouncer transaction mode
        pool_size=10,
        max_overflow=20,
    )


def get_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build a session factory bound to the given engine. No module-level singleton."""
    return async_sessionmaker(engine, expire_on_commit=False)
