"""Performance test — blast-radius p95 < 1 s (cache-hit path).

SLO
------------------------
Full transitive-closure query (depth=5, 1000-node graph) via the warmed
``closure_cache`` must complete with p95 latency < 1 s in the test container
environment.

Setup
-----
- 1000-node linear chain: N0 → N1 → … → N999 (depends_on edges).
- closure_outbox populated and ClosureRefreshWorker drained before timing.
- blast-radius from N999 (reverse, depth=5) exercises the cache-hit path.
- 10 warm-up calls discarded; 20 timed samples recorded.
- Threshold: 95th percentile of the 20 timed samples must be < 1000 ms.

Marks
-----
``@pytest.mark.perf`` + ``@pytest.mark.slow`` — excluded from unit-only CI.
To run: ``pytest tests/perf/test_perf_blast_radius.py -m perf -v``.

Note: measured in the testcontainers environment (Docker, single-node Postgres).
The 1000-node chain seeding + worker warm-up adds ~30–60 s to the fixture setup.
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
from registry.workers.closure_refresh import ClosureRefreshWorker

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)
_CHAIN_SIZE = 1000
_WARM_UP_CALLS = 5
_TIMED_CALLS = 20
_P95_TARGET_MS = 1000.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed(pg_url: str, *, slug: str) -> tuple[uuid.UUID, uuid.UUID]:
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


async def _seed_chain(pg_url: str, *, tenant_id: uuid.UUID, size: int) -> dict[str, uuid.UUID]:
    """Insert `size` entities and `size-1` depends_on edges in batches of 200."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    ids: dict[str, uuid.UUID] = {f"N{i}": uuid.uuid4() for i in range(size)}
    edge_ids: dict[str, uuid.UUID] = {f"E{i}": uuid.uuid4() for i in range(size - 1)}

    batch_size = 200
    try:
        # Insert entities in batches.
        for batch_start in range(0, size, batch_size):
            batch_end = min(batch_start + batch_size, size)
            async with factory() as session, session.begin():
                for i in range(batch_start, batch_end):
                    await session.execute(
                        text(
                            "INSERT INTO entities "
                            "(entity_id, tenant_id, entity_type, name, is_active, created_at) "
                            "VALUES (:eid, :tid, 'capability', :name, TRUE, :now)"
                        ),
                        {
                            "eid": ids[f"N{i}"],
                            "tid": tenant_id,
                            "name": f"perf-br-n{i}",
                            "now": _NOW,
                        },
                    )

        # Insert edges in batches.
        for batch_start in range(0, size - 1, batch_size):
            batch_end = min(batch_start + batch_size, size - 1)
            async with factory() as session, session.begin():
                for i in range(batch_start, batch_end):
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


async def _enqueue_edges(pg_url: str, *, tenant_id: uuid.UUID, ids: dict[str, uuid.UUID], size: int) -> None:
    """Insert closure_outbox rows for all edges in batches of 200."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    batch_size = 200
    try:
        for batch_start in range(0, size - 1, batch_size):
            batch_end = min(batch_start + batch_size, size - 1)
            async with factory() as session, session.begin():
                for i in range(batch_start, batch_end):
                    await session.execute(
                        text(
                            "INSERT INTO closure_outbox "
                            "(outbox_id, tenant_id, edge_id, enqueued_at, attempts) "
                            "VALUES (gen_random_uuid(), :tid, :eid, :now, 0)"
                        ),
                        {"tid": tenant_id, "eid": ids[f"E{i}"], "now": _NOW},
                    )
    finally:
        await engine.dispose()


def _make_session_factory(pg_url: str) -> async_sessionmaker:
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    return get_session_factory(engine)


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
# Module-level fixture: 1000-node chain with warmed closure cache
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def perf_blast_setup(pg_container: str):  # type: ignore[type-arg]
    """Seed 1000-node chain + warm closure_cache. Seeding may take ~30–60 s."""
    tenant_id, actor_id = await _seed(pg_container, slug=f"perf-blast-{uuid.uuid4().hex[:8]}")
    ids = await _seed_chain(pg_container, tenant_id=tenant_id, size=_CHAIN_SIZE)
    await _enqueue_edges(pg_container, tenant_id=tenant_id, ids=ids, size=_CHAIN_SIZE)

    sf = _make_session_factory(pg_container)
    worker = ClosureRefreshWorker(session_factory=sf, clock=FakeClock(_NOW))

    # Drain outbox: 999 edges, worker processes in batches of 50 → ~20 passes.
    total_processed = 0
    for _ in range(25):
        n = await worker.run_once()
        total_processed += n
        if n == 0:
            break

    return {
        "tenant_id": tenant_id,
        "actor_id": actor_id,
        "ids": ids,
        "pg_url": pg_container,
        "total_processed": total_processed,
    }


# ---------------------------------------------------------------------------
# Performance test
# ---------------------------------------------------------------------------


@pytest.mark.perf
@pytest.mark.slow
@pytest.mark.asyncio
async def test_blast_radius_cache_hit_p95_under_1s(perf_blast_setup: dict) -> None:
    """Blast-radius cache-hit path p95 must be < 1 s on 1000-node chain.

    Methodology:
    - 5 warm-up calls (not measured).
    - 20 timed calls via cache-hit path (closure_cache warmed in fixture).
    - 95th percentile of wall-clock times must be < 1000 ms.
    """
    setup = perf_blast_setup
    svc = _make_service(setup["pg_url"])
    ctx = TenantContext(
        tenant_id=setup["tenant_id"],
        actor_id=setup["actor_id"],
        roles=["consumer"],
    )
    ids = setup["ids"]
    root_id = ids["N999"]

    # Verify cache was warmed (fail fast if fixture broken).
    probe = await svc.get_blast_radius(
        ctx=ctx,
        entity_id=root_id,
        direction="reverse",
        depth=5,
        edge_types=["depends_on"],
    )
    assert probe.cache_hit is True, (
        f"Closure cache was not warmed (cache_hit=False). "
        f"total_processed={setup['total_processed']}. "
        "Perf test requires cache-hit path."
    )

    # Warm-up.
    for _ in range(_WARM_UP_CALLS):
        await svc.get_blast_radius(
            ctx=ctx,
            entity_id=root_id,
            direction="reverse",
            depth=5,
            edge_types=["depends_on"],
        )

    # Timed calls.
    latencies_ms: list[float] = []
    for _ in range(_TIMED_CALLS):
        t0 = time.perf_counter()
        result = await svc.get_blast_radius(
            ctx=ctx,
            entity_id=root_id,
            direction="reverse",
            depth=5,
            edge_types=["depends_on"],
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        latencies_ms.append(elapsed_ms)
        assert result.cache_hit is True, "Cache-hit path must remain consistent across calls"

    p95_ms = statistics.quantiles(latencies_ms, n=20)[18]
    mean_ms = statistics.mean(latencies_ms)
    max_ms = max(latencies_ms)

    print(
        f"\nBlast-radius cache-hit latency ({_TIMED_CALLS} calls): "
        f"mean={mean_ms:.1f}ms p95={p95_ms:.1f}ms max={max_ms:.1f}ms"
    )

    assert p95_ms < _P95_TARGET_MS, (
        f"Blast-radius p95 ({p95_ms:.1f} ms) exceeds SLO of {_P95_TARGET_MS} ms. "
        f"mean={mean_ms:.1f}ms max={max_ms:.1f}ms"
    )
