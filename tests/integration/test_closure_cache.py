"""Integration tests for closure cache worker + cache invalidation.

Contract under test
-----------------------------------------
1. Build a 10-node linear chain (A → B → C → … → J, edge rel: depends_on).
2. Manually insert outbox rows into ``closure_outbox`` for each edge.
3. Run ``ClosureRefreshWorker.run_once()`` → all outbox rows drained.
4. Assert ``closure_cache`` is populated: reverse closure from J must include
   at least I, H, G, … A.
5. Mutate an edge (soft-delete the A→B edge via raw SQL, insert new outbox row).
6. Re-run worker → cache updated; reverse closure from J still consistent.

A second test exercises the outbox-emit path via ``CatalogService.create_edge``
to verify that creating an edge inserts a ``closure_outbox`` row atomically.
"""

from __future__ import annotations

import datetime
import secrets
import uuid
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.api.auth.tokens import hash_token
from registry.service.catalog import CatalogService
from registry.service.schema import SchemaService
from registry.service.vocabulary import VocabularyService
from registry.storage.pg import get_session_factory
from registry.types import FakeClock, TenantContext
from registry.workers.closure_refresh import ClosureRefreshWorker

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)
_CHAIN_SIZE = 10  # A … J (10 nodes, 9 edges)
_LABELS = [chr(ord("A") + i) for i in range(_CHAIN_SIZE)]  # ['A','B',...,'J']


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_tenant(pg_url: str, *, slug: str) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Insert tenant + actor + API token.  Returns (tenant_id, actor_id, raw_token)."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    raw_token = secrets.token_urlsafe(24)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                    "VALUES (:tid, :slug, :slug, :now, TRUE)"
                ),
                {"tid": tenant_id, "slug": slug, "now": _NOW},
            )
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, display_name, created_at) "
                    "VALUES (:aid, :tid, :dn, :now)"
                ),
                {"aid": actor_id, "tid": tenant_id, "dn": f"actor-{slug}", "now": _NOW},
            )
            await session.execute(
                text(
                    "INSERT INTO api_tokens "
                    "(token_id, tenant_id, actor_id, token_hash, roles, created_at) "
                    "VALUES (gen_random_uuid(), :tid, :aid, :th, ARRAY['producer','consumer'], :now)"
                ),
                {
                    "tid": tenant_id,
                    "aid": actor_id,
                    "th": hash_token(raw_token),
                    "now": _NOW,
                },
            )
    finally:
        await engine.dispose()
    return tenant_id, actor_id, raw_token


async def _seed_chain(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
) -> dict[str, uuid.UUID]:
    """Insert 10 entities (A…J) and 9 edges (A→B, B→C, …, I→J).

    Returns a dict with label keys ('A'…'J', 'AB'…'IJ') mapped to UUIDs.
    """
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    ids: dict[str, uuid.UUID] = {}

    for label in _LABELS:
        ids[label] = uuid.uuid4()
    for i in range(len(_LABELS) - 1):
        edge_label = _LABELS[i] + _LABELS[i + 1]
        ids[edge_label] = uuid.uuid4()

    try:
        async with factory() as session, session.begin():
            # Insert entities
            for label in _LABELS:
                await session.execute(
                    text(
                        "INSERT INTO entities "
                        "(entity_id, tenant_id, entity_type, name, is_active, created_at) "
                        "VALUES (:eid, :tid, 'capability', :name, TRUE, :now)"
                    ),
                    {
                        "eid": ids[label],
                        "tid": tenant_id,
                        "name": f"cap-{label.lower()}",
                        "now": _NOW,
                    },
                )
            # Insert edges: A→B, B→C, …, I→J
            for i in range(len(_LABELS) - 1):
                src_label = _LABELS[i]
                dst_label = _LABELS[i + 1]
                edge_label = src_label + dst_label
                await session.execute(
                    text(
                        "INSERT INTO edges "
                        "(edge_id, tenant_id, src_entity_id, rel, dst_entity_id, "
                        " is_authoritative, t_valid_from, t_ingested_at) "
                        "VALUES (:eid, :tid, :src, 'depends_on', :dst, TRUE, :now, :now)"
                    ),
                    {
                        "eid": ids[edge_label],
                        "tid": tenant_id,
                        "src": ids[src_label],
                        "dst": ids[dst_label],
                        "now": _NOW,
                    },
                )
    finally:
        await engine.dispose()

    return ids


async def _enqueue_all_edges(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    ids: dict[str, uuid.UUID],
) -> None:
    """Insert one closure_outbox row for every edge in the chain."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            for i in range(len(_LABELS) - 1):
                edge_label = _LABELS[i] + _LABELS[i + 1]
                await session.execute(
                    text(
                        "INSERT INTO closure_outbox "
                        "(outbox_id, tenant_id, edge_id, enqueued_at, attempts) "
                        "VALUES (gen_random_uuid(), :tid, :eid, :now, 0)"
                    ),
                    {"tid": tenant_id, "eid": ids[edge_label], "now": _NOW},
                )
    finally:
        await engine.dispose()


def _make_session_factory(pg_url: str) -> async_sessionmaker:
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    return get_session_factory(engine)


def _make_worker(pg_url: str) -> ClosureRefreshWorker:
    sf = _make_session_factory(pg_url)
    return ClosureRefreshWorker(session_factory=sf, clock=FakeClock(_NOW))


async def _count_closure_rows(pg_url: str, tenant_id: uuid.UUID) -> int:
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await session.execute(
                text("SELECT COUNT(*) FROM closure_cache WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
            return result.scalar_one()
    finally:
        await engine.dispose()


async def _count_outbox_rows(pg_url: str, tenant_id: uuid.UUID) -> int:
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await session.execute(
                text("SELECT COUNT(*) FROM closure_outbox WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
            return result.scalar_one()
    finally:
        await engine.dispose()


async def _fetch_closure_rows(
    pg_url: str,
    tenant_id: uuid.UUID,
    root_entity_id: uuid.UUID,
    direction: str,
) -> list[dict[str, Any]]:
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await session.execute(
                text(
                    "SELECT member_entity_id, depth FROM closure_cache "
                    "WHERE tenant_id = :tid "
                    "  AND root_entity_id = :root "
                    "  AND direction = :dir "
                    "ORDER BY depth"
                ),
                {"tid": tenant_id, "root": root_entity_id, "dir": direction},
            )
            return [dict(r) for r in result.mappings().all()]
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Module-scoped fixture: one chain shared across all tests in this module
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def chain_setup(pg_container: str):  # type: ignore[type-arg]
    """Seed tenant + 10-cap chain for the module.  Returns setup dict."""
    tenant_id, actor_id, raw_token = await _seed_tenant(pg_container, slug=f"t06-{uuid.uuid4().hex[:8]}")
    ids = await _seed_chain(pg_container, tenant_id=tenant_id)
    return {
        "tenant_id": tenant_id,
        "actor_id": actor_id,
        "raw_token": raw_token,
        "ids": ids,
        "pg_url": pg_container,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_drains_outbox_and_populates_cache(chain_setup: dict) -> None:
    """After run_once(), all outbox rows are consumed and closure_cache is populated.

    Verifies:
    - run_once() returns the number of edge outbox rows processed (9).
    - closure_outbox is empty afterward.
    - closure_cache has rows for the tenant.
    """
    setup = chain_setup
    pg_url: str = setup["pg_url"]
    tenant_id: uuid.UUID = setup["tenant_id"]
    ids: dict[str, uuid.UUID] = setup["ids"]

    # Enqueue all 9 edges.
    await _enqueue_all_edges(pg_url, tenant_id=tenant_id, ids=ids)
    assert await _count_outbox_rows(pg_url, tenant_id) == 9

    worker = _make_worker(pg_url)
    processed = await worker.run_once()

    # All 9 outbox rows should have been processed.
    assert processed == 9, f"expected 9 processed, got {processed}"

    # Outbox should be empty.
    remaining = await _count_outbox_rows(pg_url, tenant_id)
    assert remaining == 0, f"expected 0 outbox rows remaining, got {remaining}"

    # Closure cache must have been populated.
    cache_count = await _count_closure_rows(pg_url, tenant_id)
    assert cache_count > 0, "closure_cache must be non-empty after worker run"


@pytest.mark.asyncio
async def test_reverse_closure_from_J_contains_nearby_ancestors(chain_setup: dict) -> None:
    """closure_cache reverse closure from J must include its 5-hop ancestors.

    _MAX_DEPTH=5 caps traversal at 5 hops from root.  In the 10-node chain
    A→B→…→J, reverse from J can reach at most I(1), H(2), G(3), F(4), E(5).
    Nodes D through A are beyond the depth cap.

    Enqueues all chain edges and runs the worker so the cache is populated,
    then asserts the cache result for J's 5 closest reverse-reachable ancestors.
    """
    setup = chain_setup
    pg_url: str = setup["pg_url"]
    tenant_id: uuid.UUID = setup["tenant_id"]
    ids: dict[str, uuid.UUID] = setup["ids"]

    # Ensure outbox rows exist and run the worker (idempotent if already run).
    await _enqueue_all_edges(pg_url, tenant_id=tenant_id, ids=ids)
    worker = _make_worker(pg_url)
    await worker.run_once()

    # J is the last node (index 9 in _LABELS = 'J').
    j_id = ids["J"]
    rows = await _fetch_closure_rows(pg_url, tenant_id, j_id, "reverse")

    member_ids = {r["member_entity_id"] for r in rows}

    # Nodes reachable within depth 5 from J (reverse): I, H, G, F, E.
    for label in ["I", "H", "G", "F", "E"]:
        assert ids[label] in member_ids, f"reverse closure from J missing {label} at depth ≤ 5 (id={ids[label]})"

    # The cache must be non-empty for J.
    assert len(member_ids) >= 5, f"reverse closure from J expected >= 5 nodes, got {len(member_ids)}"


@pytest.mark.asyncio
async def test_forward_closure_from_A_contains_nearby_successors(chain_setup: dict) -> None:
    """closure_cache forward closure from A must include its 5-hop successors.

    _MAX_DEPTH=5 caps traversal at 5 hops.  Forward from A can reach B(1),
    C(2), D(3), E(4), F(5) — nodes G, H, I, J are beyond depth 5.
    """
    setup = chain_setup
    pg_url: str = setup["pg_url"]
    tenant_id: uuid.UUID = setup["tenant_id"]
    ids: dict[str, uuid.UUID] = setup["ids"]

    # Ensure outbox rows exist and run the worker (idempotent if already run).
    await _enqueue_all_edges(pg_url, tenant_id=tenant_id, ids=ids)
    worker = _make_worker(pg_url)
    await worker.run_once()

    a_id = ids["A"]
    rows = await _fetch_closure_rows(pg_url, tenant_id, a_id, "forward")
    member_ids = {r["member_entity_id"] for r in rows}

    # Nodes reachable within depth 5 from A (forward): B, C, D, E, F.
    for label in ["B", "C", "D", "E", "F"]:
        assert ids[label] in member_ids, f"forward closure from A missing {label} at depth ≤ 5 (id={ids[label]})"

    assert len(member_ids) >= 5, f"forward closure from A expected >= 5 nodes, got {len(member_ids)}"


@pytest.mark.asyncio
async def test_mutate_edge_and_rerun_worker_updates_cache(chain_setup: dict) -> None:
    """After soft-deleting the A→B edge, re-running the worker updates the cache.

    The forward closure from A should no longer reach B, C, …, J after
    invalidation.  We enqueue the outbox row manually (as catalog.py would),
    re-run the worker, and assert the closure is updated.
    """
    setup = chain_setup
    pg_url: str = setup["pg_url"]
    tenant_id: uuid.UUID = setup["tenant_id"]
    ids: dict[str, uuid.UUID] = setup["ids"]

    # Soft-delete edge A→B.
    ab_edge_id = ids["AB"]
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    invalidated_at = _NOW + datetime.timedelta(hours=1)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE edges SET t_valid_to = :ts, t_invalidated_at = :ts "
                    "WHERE edge_id = :eid AND tenant_id = :tid"
                ),
                {"ts": invalidated_at, "eid": ab_edge_id, "tid": tenant_id},
            )
            # Insert outbox row (mimics catalog.py emit).
            await session.execute(
                text(
                    "INSERT INTO closure_outbox "
                    "(outbox_id, tenant_id, edge_id, enqueued_at, attempts) "
                    "VALUES (gen_random_uuid(), :tid, :eid, :now, 0)"
                ),
                {"tid": tenant_id, "eid": ab_edge_id, "now": invalidated_at},
            )
    finally:
        await engine.dispose()

    # Run the worker to process the invalidation.
    worker = _make_worker(pg_url)
    processed = await worker.run_once()
    assert processed >= 1, f"expected >= 1 processed after edge mutation, got {processed}"

    # After invalidation, forward closure from A should NOT reach B, C, …, J
    # (since A→B is the only path out of A).
    a_id = ids["A"]
    rows = await _fetch_closure_rows(pg_url, tenant_id, a_id, "forward")
    member_ids = {r["member_entity_id"] for r in rows}

    # A no longer has any outgoing active edges, so its closure must be empty.
    assert len(member_ids) == 0, (
        f"forward closure from A should be empty after removing A→B edge, " f"but found: {member_ids}"
    )


@pytest.mark.asyncio
async def test_create_edge_via_catalog_service_emits_outbox_row(pg_container: str) -> None:
    """catalog.create_edge must insert a row into closure_outbox atomically.

    This test uses CatalogService directly (no HTTP) to confirm the outbox-emit
    path works end-to-end — catalog.create_edge must insert the outbox row atomically.
    """
    pg_url = pg_container
    tenant_id, actor_id, _ = await _seed_tenant(pg_url, slug=f"t06-emit-{uuid.uuid4().hex[:8]}")

    # Create two entities for the edge.
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    src_id = uuid.uuid4()
    dst_id = uuid.uuid4()
    try:
        async with factory() as session, session.begin():
            for eid, name in ((src_id, "src-cap"), (dst_id, "dst-cap")):
                await session.execute(
                    text(
                        "INSERT INTO entities "
                        "(entity_id, tenant_id, entity_type, name, is_active, created_at) "
                        "VALUES (:eid, :tid, 'capability', :name, TRUE, :now)"
                    ),
                    {"eid": eid, "tid": tenant_id, "name": name, "now": _NOW},
                )
            # Seed 'depends_on' vocab for this tenant (already system-seeded in migration
            # but must also exist for this tenant if vocab checks are tenant-scoped).
            await session.execute(
                text(
                    "INSERT INTO vocabulary_values (tenant_id, kind, value, is_system) "
                    "VALUES (:tid, 'edge_rel', 'depends_on', FALSE) "
                    "ON CONFLICT DO NOTHING"
                ),
                {"tid": tenant_id},
            )
    finally:
        await engine.dispose()

    # Build CatalogService.
    svc_engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    svc_factory = get_session_factory(svc_engine)
    clock = FakeClock(_NOW)
    vocab = VocabularyService(svc_factory)
    schema = SchemaService(svc_factory, clock)
    catalog = CatalogService(
        session_factory=svc_factory,
        clock=clock,
        vocabulary=vocab,
        schema=schema,
    )
    ctx = TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["producer"])

    # Count outbox rows before.
    before = await _count_outbox_rows(pg_url, tenant_id)

    # Create edge via CatalogService.
    await catalog.create_edge(
        ctx=ctx,
        src_entity_id=src_id,
        rel="depends_on",
        dst_entity_id=dst_id,
    )

    # Count outbox rows after.
    after = await _count_outbox_rows(pg_url, tenant_id)
    assert after == before + 1, (
        f"expected closure_outbox to grow by 1 after create_edge, " f"got before={before} after={after}"
    )

    await svc_engine.dispose()


@pytest.mark.asyncio
async def test_evict_stale_removes_old_rows(pg_container: str) -> None:
    """evict_stale() must delete closure_cache rows older than 90 days."""
    pg_url = pg_container
    tenant_id, _, _ = await _seed_tenant(pg_url, slug=f"t06-evict-{uuid.uuid4().hex[:8]}")

    # Insert two entity stubs for the cache row FKs.
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    root_id = uuid.uuid4()
    member_id = uuid.uuid4()
    stale_ts = _NOW - datetime.timedelta(days=91)
    fresh_ts = _NOW

    try:
        async with factory() as session, session.begin():
            for eid, name in ((root_id, "evict-root"), (member_id, "evict-member")):
                await session.execute(
                    text(
                        "INSERT INTO entities "
                        "(entity_id, tenant_id, entity_type, name, is_active, created_at) "
                        "VALUES (:eid, :tid, 'capability', :name, TRUE, :now)"
                    ),
                    {"eid": eid, "tid": tenant_id, "name": name, "now": _NOW},
                )
            # Insert a stale row (91 days old).
            await session.execute(
                text(
                    "INSERT INTO closure_cache "
                    "(cache_id, tenant_id, root_entity_id, member_entity_id, "
                    " direction, depth, edge_path, edge_rels, refreshed_at) "
                    "VALUES (gen_random_uuid(), :tid, :root, :member, "
                    "        'forward', 1, ARRAY[]::uuid[], ARRAY[]::text[], :ts)"
                ),
                {"tid": tenant_id, "root": root_id, "member": member_id, "ts": stale_ts},
            )
            # Insert a fresh row.
            await session.execute(
                text(
                    "INSERT INTO closure_cache "
                    "(cache_id, tenant_id, root_entity_id, member_entity_id, "
                    " direction, depth, edge_path, edge_rels, refreshed_at) "
                    "VALUES (gen_random_uuid(), :tid, :root, :member, "
                    "        'reverse', 1, ARRAY[]::uuid[], ARRAY[]::text[], :ts)"
                ),
                {"tid": tenant_id, "root": member_id, "member": root_id, "ts": fresh_ts},
            )
    finally:
        await engine.dispose()

    # Worker clock set to _NOW so cutoff = _NOW - 90d; stale_ts < cutoff.
    sf = _make_session_factory(pg_url)
    worker = ClosureRefreshWorker(session_factory=sf, clock=FakeClock(_NOW))
    deleted = await worker.evict_stale()

    assert deleted >= 1, f"expected >= 1 stale row deleted, got {deleted}"

    remaining = await _count_closure_rows(pg_url, tenant_id)
    assert remaining >= 1, "fresh row must remain after eviction"
