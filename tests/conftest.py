"""Shared integration fixtures: testcontainers Postgres + pgvector and FakeClock.

Spins one container per pytest session. The session-scoped fixture runs Alembic
`upgrade head` against the container before any test executes; the per-test
`db_session` fixture wraps a transaction that is rolled back at teardown so
tests are isolated without re-applying migrations.
"""

from __future__ import annotations

import asyncio
import datetime
import os
import subprocess
import sys
from collections.abc import AsyncGenerator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from testcontainers.postgres import PostgresContainer

from registry.config import Settings
from registry.types import FakeClock


def _to_async_url(jdbc_like: str) -> str:
    """Translate testcontainers' default psycopg2 URL into an asyncpg URL."""
    return jdbc_like.replace("postgresql+psycopg2://", "postgresql+asyncpg://").replace(
        "postgresql://", "postgresql+asyncpg://"
    )


@pytest.fixture(scope="session")
def pg_container() -> Iterator[str]:
    """Start a Postgres 16 + pgvector container for the whole test session."""
    container = PostgresContainer(image="pgvector/pgvector:pg16", username="postgres", password="password")
    container.start()
    try:
        url = _to_async_url(container.get_connection_url())

        env = {**os.environ, "DATABASE_URL": url}
        # tests/conftest.py → project root is one level up.
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=project_root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            msg = f"alembic upgrade head failed:\n{result.stdout}\n{result.stderr}"
            raise RuntimeError(msg)
        yield url
    finally:
        container.stop()


@pytest.fixture(scope="session")
def app_settings(pg_container: str) -> Settings:
    return Settings(
        database_url=pg_container,
        pgbouncer_url=pg_container,
        scheduler_jobstore_url=pg_container,
    )


@pytest_asyncio.fixture
async def db_session(pg_container: str) -> AsyncGenerator[AsyncSession, None]:
    """Per-test AsyncSession against the shared container."""
    engine = create_async_engine(
        pg_container,
        connect_args={"prepared_statement_cache_size": 0},  # required for asyncpg + pgbouncer compatibility
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock(datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC))


@pytest.fixture
def event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()
