"""Shared integration fixtures: testcontainers Postgres + pgvector, FakeClock,
and respx cassette transport for connector HTTP mocking.

Spins one container per pytest session. The session-scoped fixture runs Alembic
`upgrade head` against the container before any test executes; the per-test
`db_session` fixture wraps a transaction that is rolled back at teardown so
tests are isolated without re-applying migrations.

Cassette infrastructure
-----------------------
``respx_cassette(connector_name)`` is a session-scoped factory fixture.  Call
it inside a test or fixture to get a ``respx.MockRouter`` pre-loaded with all
``cassette_*.json`` files found under
``tests/fixtures/connectors/<connector_name>/``.

Cassette file schema (each file is one HTTP exchange)::

    {
        "request": {
            "method": "GET",
            "url": "https://...",
            "headers": {"Authorization": "Bearer test-token"}
        },
        "response": {
            "status": 200,
            "headers": {"Content-Type": "application/json"},
            "body": <string or JSON-serialisable object>
        }
    }

Set ``REFRESH_CASSETTES=1`` to enable pass-through mode (no mocking); real
network calls are made and results can be captured to update the cassette files.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import subprocess
import sys
from collections.abc import AsyncGenerator, Callable, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
import respx
from httpx import Response
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from testcontainers.postgres import PostgresContainer

from registry.config import Settings
from registry.types import FakeClock

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIXTURES_ROOT = Path(__file__).parent.parent / "fixtures" / "connectors"


def _to_async_url(jdbc_like: str) -> str:
    """Translate testcontainers' default psycopg2 URL into an asyncpg URL."""
    return jdbc_like.replace("postgresql+psycopg2://", "postgresql+asyncpg://").replace(
        "postgresql://", "postgresql+asyncpg://"
    )


def _load_cassette(path: Path) -> dict:
    """Load and parse a single cassette JSON file."""
    with path.open() as fh:
        return json.load(fh)


def _response_from_cassette(entry: dict) -> Response:
    """Build an httpx.Response from the ``response`` block of a cassette entry."""
    resp = entry["response"]
    body = resp["body"]
    if not isinstance(body, str):
        body = json.dumps(body)
    return Response(
        status_code=resp.get("status", 200),
        headers=resp.get("headers", {}),
        text=body,
    )


# ---------------------------------------------------------------------------
# Cassette fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def respx_cassette() -> Callable[[str], respx.MockRouter]:
    """Return a factory that loads cassette files for a named connector.

    Usage::

        def test_something(respx_cassette):
            router = respx_cassette("openapi")
            with router:
                # all httpx calls are intercepted
                ...

    When ``REFRESH_CASSETTES=1`` is set the returned router is in pass-through
    mode — every call is forwarded to the real network.  This is intentionally
    not activated in CI.
    """
    refresh = os.environ.get("REFRESH_CASSETTES", "").strip() == "1"

    def _factory(connector_name: str) -> respx.MockRouter:
        router = respx.MockRouter(assert_all_called=False)

        if refresh:
            # Pass-through: forward to real network.  Callers can capture
            # responses and overwrite the cassette files.
            router.pass_through(lambda request: True)  # type: ignore[arg-type]
            return router

        cassette_dir = _FIXTURES_ROOT / connector_name
        if not cassette_dir.is_dir():
            raise FileNotFoundError(f"No cassette directory for connector '{connector_name}': {cassette_dir}")

        cassette_files = sorted(cassette_dir.glob("cassette_*.json"))
        if not cassette_files:
            raise FileNotFoundError(f"No cassette_*.json files found in {cassette_dir}")

        for cassette_path in cassette_files:
            entry = _load_cassette(cassette_path)
            req = entry["request"]
            method = req["method"].upper()
            url = req["url"]
            response = _response_from_cassette(entry)
            router.route(method=method, url=url).mock(return_value=response)

        return router

    return _factory


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def pg_container() -> Iterator[str]:
    """Start a Postgres 16 + pgvector container for the whole test session."""
    container = PostgresContainer(image="pgvector/pgvector:pg16", username="postgres", password="password")
    container.start()
    try:
        url = _to_async_url(container.get_connection_url())

        env = {**os.environ, "DATABASE_URL": url}
        # This file is at tests/integration/conftest.py; project root is three levels up.
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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
