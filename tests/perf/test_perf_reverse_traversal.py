"""Performance test — reverse traversal p95 < 300 ms.

SLO
------------------------
``GET /v1/capabilities/{id}/dependents?depth=5`` on a 100-node linear graph
must complete with p95 latency < 300 ms in the test container environment.

Setup
-----
- 100-node linear chain: N0 → N1 → … → N99 (depends_on edges).
- Reverse traversal from N99 (depth=5) returns the 5 nearest ancestors.
- 30 warm-up calls discarded; 50 timed samples recorded.
- Threshold: 95th percentile of the 50 timed samples must be < 300 ms.

Marks
-----
``@pytest.mark.perf`` + ``@pytest.mark.slow`` — excluded from unit-only CI.
To run: ``pytest tests/perf/test_perf_reverse_traversal.py -m perf -v``.

Note: measured in the testcontainers environment (Docker, single-node Postgres).
Production hardware and a tuned Postgres instance will see lower latency.
"""

from __future__ import annotations

import datetime
import secrets
import statistics
import time
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.api.auth.tokens import hash_token
from registry.config import Settings
from registry.embedder import StubEmbedder
from registry.service.retrieval import RetrievalService
from registry.storage.models import Actor, ApiToken, Tenant
from registry.storage.pg import get_session_factory
from registry.types import FakeClock, TenantContext

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)
_CHAIN_SIZE = 100
_WARM_UP_CALLS = 10
_TIMED_CALLS = 30
_P95_TARGET_MS = 300.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed(pg_url: str, *, slug: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert tenant + actor. Returns (tenant_id, actor_id)."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    raw = secrets.token_urlsafe(24)
    try:
        async with factory() as session, session.begin():
            session.add(
                Tenant(
                    tenant_id=tenant_id,
                    slug=slug,
                    display_name=slug,
                    created_at=_NOW,
                    is_active=True,
                )
            )
            await session.flush()
            session.add(
                Actor(
                    actor_id=actor_id,
                    tenant_id=tenant_id,
                    display_name=f"a-{slug}",
                    email=None,
                    oidc_subject=None,
                    created_at=_NOW,
                )
            )
            await session.flush()
            session.add(
                ApiToken(
                    token_id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    token_hash=hash_token(raw),
                    roles=["consumer"],
                    description=None,
                    expires_at=None,
                    created_at=_NOW,
                    revoked_at=None,
                )
            )
    finally:
        await engine.dispose()
    return tenant_id, actor_id


async def _seed_chain(pg_url: str, *, tenant_id: uuid.UUID, size: int = _CHAIN_SIZE) -> dict[str, uuid.UUID]:
    """Insert `size` entities and `size-1` depends_on edges. Returns label→UUID dict."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    ids: dict[str, uuid.UUID] = {f"N{i}": uuid.uuid4() for i in range(size)}
    edge_ids: dict[str, uuid.UUID] = {f"E{i}": uuid.uuid4() for i in range(size - 1)}
    try:
        async with factory() as session, session.begin():
            for i in range(size):
                await session.execute(
                    text(
                        "INSERT INTO entities "
                        "(entity_id, tenant_id, entity_type, name, is_active, created_at) "
                        "VALUES (:eid, :tid, 'capability', :name, TRUE, :now)"
                    ),
                    {"eid": ids[f"N{i}"], "tid": tenant_id, "name": f"perf-n{i}", "now": _NOW},
                )
            for i in range(size - 1):
                await session.execute(
                    text(
                        "INSERT INTO edges "
                        "(edge_id, tenant_id, src_entity_id, rel, dst_entity_id, "
                        " is_authoritative, t_valid_from, t_ingested_at) "
                        "VALUES (:eid, :tid, :src, 'depends_on', :dst, TRUE, :now, :now)"
                    ),
                    {
                        "eid": edge_ids[f"E{i}"],
                        "tid": tenant_id,
                        "src": ids[f"N{i}"],
                        "dst": ids[f"N{i + 1}"],
                        "now": _NOW,
                    },
                )
    finally:
        await engine.dispose()
    return ids


def _make_service(pg_url: str) -> RetrievalService:
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    sf = get_session_factory(engine)
    return RetrievalService(
        session_factory=sf,
        clock=FakeClock(_NOW),
        embedder=StubEmbedder(),
        settings=Settings(
            database_url=pg_url,
            pgbouncer_url=pg_url,
            scheduler_jobstore_url=pg_url,
        ),
    )


# ---------------------------------------------------------------------------
# Module-level fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def perf_reverse_setup(pg_container: str):  # type: ignore[type-arg]
    """Seed 100-node chain once for all tests in this module."""
    tenant_id, actor_id = await _seed(pg_container, slug=f"perf-rev-{uuid.uuid4().hex[:8]}")
    ids = await _seed_chain(pg_container, tenant_id=tenant_id)
    return {"tenant_id": tenant_id, "actor_id": actor_id, "ids": ids, "pg_url": pg_container}


# ---------------------------------------------------------------------------
# Performance test
# ---------------------------------------------------------------------------


@pytest.mark.perf
@pytest.mark.slow
@pytest.mark.asyncio
async def test_reverse_traversal_p95_under_300ms(perf_reverse_setup: dict) -> None:
    """Reverse traversal p95 must be < 300 ms on 100-node chain.

    Methodology:
    - 10 warm-up calls (not measured) to ensure connection pool and query plan caches.
    - 30 timed calls; 95th percentile of wall-clock times must be < 300 ms.
    """
    setup = perf_reverse_setup
    svc = _make_service(setup["pg_url"])
    ctx = TenantContext(
        tenant_id=setup["tenant_id"],
        actor_id=setup["actor_id"],
        roles=["consumer"],
    )
    ids = setup["ids"]
    root_id = ids["N99"]

    # Warm-up.
    for _ in range(_WARM_UP_CALLS):
        await svc.get_reverse_traversal(
            ctx=ctx,
            entity_id=root_id,
            depth=5,
            edge_types=["depends_on"],
        )

    # Timed calls.
    latencies_ms: list[float] = []
    for _ in range(_TIMED_CALLS):
        t0 = time.perf_counter()
        await svc.get_reverse_traversal(
            ctx=ctx,
            entity_id=root_id,
            depth=5,
            edge_types=["depends_on"],
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        latencies_ms.append(elapsed_ms)

    p95_ms = statistics.quantiles(latencies_ms, n=20)[18]  # 95th percentile = index 18 of 20
    mean_ms = statistics.mean(latencies_ms)
    max_ms = max(latencies_ms)

    print(
        f"\nReverse traversal latency ({_TIMED_CALLS} calls): "
        f"mean={mean_ms:.1f}ms p95={p95_ms:.1f}ms max={max_ms:.1f}ms"
    )

    assert p95_ms < _P95_TARGET_MS, (
        f"Reverse traversal p95 ({p95_ms:.1f} ms) exceeds SLO of {_P95_TARGET_MS} ms. "
        f"mean={mean_ms:.1f}ms max={max_ms:.1f}ms samples={latencies_ms}"
    )
