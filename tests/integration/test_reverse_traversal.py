"""Integration tests for reverse traversal service + REST endpoint.

Contract under test
-----------------------------------------------------------------
Five-capability chain:  A -> B -> C -> D -> E  (edge rel: depends_on)

Forward from A  (get_dependencies): B(1), C(2), D(3), E(4)
Reverse from E  (get_reverse_traversal): D(1), C(2), B(3), A(4)

Both directions must return symmetric sets of nodes (same entities, mirrored
depths).  ``cache_hit`` must be ``False`` when the closure cache is not warmed.
``version_satisfied`` must be ``True`` for every edge when no version predicates
are configured.

The REST endpoint ``GET /v1/capabilities/{entity_id}/dependents`` must return
HTTP 200 with a ``TraversalResultResponse`` body that matches the service result.

Visibility: same-tenant tests -- all nodes belong to the calling tenant, so all
are returned.
"""

from __future__ import annotations

import datetime
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.config import Settings
from registry.embedder import StubEmbedder
from registry.service.retrieval import RetrievalService
from registry.storage.pg import get_session_factory
from registry.types import FakeClock, TemporalFilter, TenantContext, TraversalResult
from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    bearer_headers,
    patch_validator_for_actor,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_tenant(pg_url: str, *, slug: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert tenant + actor rows.  Returns (tenant_id, actor_id).

    No api_token row is written; service-layer tests build TenantContext
    directly and REST tests authenticate via the entitlement auth harness.
    """
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    oidc_subject = f"oidc-sub-{slug}-{actor_id.hex[:8]}"
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
                    "INSERT INTO actors (actor_id, tenant_id, oidc_subject, display_name, created_at) "
                    "VALUES (:aid, :tid, :sub, :dn, :now)"
                ),
                {"aid": actor_id, "tid": tenant_id, "sub": oidc_subject, "dn": f"actor-{slug}", "now": _NOW},
            )
    finally:
        await engine.dispose()
    return tenant_id, actor_id


async def _seed_five_cap_chain(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
) -> dict[str, uuid.UUID]:
    """Insert entities A->B->C->D->E and depends_on edges.

    Returns dict with keys 'A', 'B', 'C', 'D', 'E' mapping to entity_ids,
    and 'AB', 'BC', 'CD', 'DE' mapping to edge_ids.
    """
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    ids: dict[str, uuid.UUID] = {}
    for name in ("A", "B", "C", "D", "E"):
        ids[name] = uuid.uuid4()
    for pair in ("AB", "BC", "CD", "DE"):
        ids[pair] = uuid.uuid4()

    try:
        async with factory() as session, session.begin():
            # Insert entities
            for label in ("A", "B", "C", "D", "E"):
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

            # Insert edges: A->B, B->C, C->D, D->E
            chain = [("A", "B", "AB"), ("B", "C", "BC"), ("C", "D", "CD"), ("D", "E", "DE")]
            for src_label, dst_label, edge_label in chain:
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


# ---------------------------------------------------------------------------
# Module-scoped fixture: seed the chain data shared by service-layer tests
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def chain_setup(pg_container: str):  # type: ignore[type-arg]
    """Seed tenant + 5-cap chain once for the whole module.

    Returns dict with keys tenant_id, actor_id, ids, pg_url.
    Service-layer tests construct TenantContext directly from these.
    """
    slug = f"t05-{uuid.uuid4().hex[:8]}"
    tenant_id, actor_id = await _seed_tenant(pg_container, slug=slug)
    chain_ids = await _seed_five_cap_chain(pg_container, tenant_id=tenant_id)
    return {
        "tenant_id": tenant_id,
        "actor_id": actor_id,
        "ids": chain_ids,
        "pg_url": pg_container,
        "slug": slug,
    }


def _make_retrieval_service(pg_url: str) -> RetrievalService:
    """Build a real RetrievalService wired to the test container."""
    from sqlalchemy.ext.asyncio import create_async_engine  # noqa: PLC0415

    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    session_factory = get_session_factory(engine)
    clock = FakeClock(_NOW)
    embedder = StubEmbedder()
    return RetrievalService(
        session_factory=session_factory,
        clock=clock,
        embedder=embedder,
        settings=Settings(
            database_url=pg_url,
            pgbouncer_url=pg_url,
            scheduler_jobstore_url=pg_url,
        ),
    )


# ---------------------------------------------------------------------------
# Service-layer tests (direct call, no HTTP)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reverse_traversal_from_E_returns_D_C_B_A(chain_setup: dict) -> None:
    """Reverse traversal from E in A->B->C->D->E must return D, C, B, A as nodes."""
    svc = _make_retrieval_service(chain_setup["pg_url"])
    ctx = TenantContext(
        tenant_id=chain_setup["tenant_id"],
        actor_id=chain_setup["actor_id"],
        roles=["consumer"],
    )
    ids = chain_setup["ids"]

    result: TraversalResult = await svc.get_reverse_traversal(
        ctx=ctx,
        entity_id=ids["E"],
        depth=5,
        edge_types=["depends_on"],
    )

    assert isinstance(result, TraversalResult)
    assert result.direction == "reverse"
    assert result.root_entity_id == ids["E"]
    assert result.cache_hit is False
    assert result.depth == 5

    node_ids = {n.entity_id for n in result.nodes}
    assert ids["D"] in node_ids, "D must be reachable from E in reverse"
    assert ids["C"] in node_ids, "C must be reachable from E in reverse"
    assert ids["B"] in node_ids, "B must be reachable from E in reverse"
    assert ids["A"] in node_ids, "A must be reachable from E in reverse"
    assert ids["E"] not in node_ids, "root must not appear in reverse traversal nodes"


@pytest.mark.asyncio
async def test_reverse_traversal_from_E_node_count(chain_setup: dict) -> None:
    """Reverse from E must return exactly 4 nodes (D, C, B, A)."""
    svc = _make_retrieval_service(chain_setup["pg_url"])
    ctx = TenantContext(
        tenant_id=chain_setup["tenant_id"],
        actor_id=chain_setup["actor_id"],
        roles=["consumer"],
    )
    ids = chain_setup["ids"]

    result = await svc.get_reverse_traversal(
        ctx=ctx,
        entity_id=ids["E"],
        depth=5,
        edge_types=["depends_on"],
    )

    assert len(result.nodes) == 4, (
        f"expected 4 nodes in reverse from E, got {len(result.nodes)}: " f"{[str(n.entity_id) for n in result.nodes]}"
    )


@pytest.mark.asyncio
async def test_forward_and_reverse_symmetric_node_sets(chain_setup: dict) -> None:
    """Forward from A and reverse from E must cover the same intermediate set.

    Forward from A returns {B, C, D, E}.
    Reverse from E returns {D, C, B, A}.
    Union minus the respective roots = {B, C, D}.
    """
    svc = _make_retrieval_service(chain_setup["pg_url"])
    ctx = TenantContext(
        tenant_id=chain_setup["tenant_id"],
        actor_id=chain_setup["actor_id"],
        roles=["consumer"],
    )
    ids = chain_setup["ids"]

    # Forward from A
    fwd = await svc.get_dependencies(
        ctx=ctx,
        entity_id=ids["A"],
        depth=5,
        temporal_filter=TemporalFilter(as_of=None),
    )
    fwd_dst_ids = {e.dst_entity_id for e in fwd}

    # Reverse from E
    rev = await svc.get_reverse_traversal(
        ctx=ctx,
        entity_id=ids["E"],
        depth=5,
        edge_types=["depends_on"],
    )
    rev_node_ids = {n.entity_id for n in rev.nodes}

    # Both must contain B, C, D (the interior nodes)
    interior = {ids["B"], ids["C"], ids["D"]}
    assert interior.issubset(fwd_dst_ids), f"forward from A missing interior nodes: {interior - fwd_dst_ids}"
    assert interior.issubset(rev_node_ids), f"reverse from E missing interior nodes: {interior - rev_node_ids}"

    # Forward includes E but not A (root); reverse includes A but not E (root)
    assert ids["E"] in fwd_dst_ids
    assert ids["A"] not in fwd_dst_ids
    assert ids["A"] in rev_node_ids
    assert ids["E"] not in rev_node_ids


@pytest.mark.asyncio
async def test_reverse_traversal_depth_cap(chain_setup: dict) -> None:
    """Depth=1 from E must return only D (1 hop)."""
    svc = _make_retrieval_service(chain_setup["pg_url"])
    ctx = TenantContext(
        tenant_id=chain_setup["tenant_id"],
        actor_id=chain_setup["actor_id"],
        roles=["consumer"],
    )
    ids = chain_setup["ids"]

    result = await svc.get_reverse_traversal(
        ctx=ctx,
        entity_id=ids["E"],
        depth=1,
        edge_types=["depends_on"],
    )

    node_ids = {n.entity_id for n in result.nodes}
    assert ids["D"] in node_ids, "depth=1 from E must reach D"
    assert ids["C"] not in node_ids, "depth=1 from E must not reach C"
    assert ids["B"] not in node_ids, "depth=1 from E must not reach B"
    assert ids["A"] not in node_ids, "depth=1 from E must not reach A"


@pytest.mark.asyncio
async def test_reverse_traversal_cache_hit_is_false(chain_setup: dict) -> None:
    """cache_hit must be False when the closure cache has not been warmed."""
    svc = _make_retrieval_service(chain_setup["pg_url"])
    ctx = TenantContext(
        tenant_id=chain_setup["tenant_id"],
        actor_id=chain_setup["actor_id"],
        roles=["consumer"],
    )
    ids = chain_setup["ids"]

    result = await svc.get_reverse_traversal(
        ctx=ctx,
        entity_id=ids["E"],
        depth=5,
        edge_types=["depends_on"],
    )

    assert result.cache_hit is False, "cache_hit must be False when closure cache is not warmed"


@pytest.mark.asyncio
async def test_reverse_traversal_version_satisfied_stub(chain_setup: dict) -> None:
    """version_satisfied must be all True when no version predicates are configured."""
    svc = _make_retrieval_service(chain_setup["pg_url"])
    ctx = TenantContext(
        tenant_id=chain_setup["tenant_id"],
        actor_id=chain_setup["actor_id"],
        roles=["consumer"],
    )
    ids = chain_setup["ids"]

    result = await svc.get_reverse_traversal(
        ctx=ctx,
        entity_id=ids["E"],
        depth=5,
        edge_types=["depends_on"],
    )

    for edge_id, satisfied in result.version_satisfied.items():
        assert (
            satisfied is True
        ), f"version_satisfied[{edge_id}] must be True when no predicates configured, got {satisfied}"


@pytest.mark.asyncio
async def test_reverse_traversal_empty_graph(chain_setup: dict) -> None:
    """Leaf with no inbound edges returns empty nodes + no error."""
    svc = _make_retrieval_service(chain_setup["pg_url"])
    ctx = TenantContext(
        tenant_id=chain_setup["tenant_id"],
        actor_id=chain_setup["actor_id"],
        roles=["consumer"],
    )
    # A has no inbound edges in the chain (it depends on nothing upstream)
    ids = chain_setup["ids"]

    result = await svc.get_reverse_traversal(
        ctx=ctx,
        entity_id=ids["A"],
        depth=5,
        edge_types=["depends_on"],
    )

    assert result.nodes == [], f"A has no inbound depends_on edges; expected empty nodes, got {result.nodes}"
    assert result.cache_hit is False


# ---------------------------------------------------------------------------
# REST endpoint tests (HTTP via httpx AsyncClient + auth harness)
#
# Each test opens its own harness so the FastAPI lifespan is fresh and the
# module-scoped chain_setup fixture's engine is not shared with the app.
# The harness JIT-materialises the tenant from the chain_setup slug, so
# entity rows seeded above are visible to the authenticated requests.
# ---------------------------------------------------------------------------


async def _make_http_client(pg_url: str, slug: str):  # type: ignore[return]
    """Yield (harness, persona, AsyncClient) for one REST test."""
    async with EntitlementAuthHarness(pg_url) as harness:
        persona = harness.add_persona(slug, roles=["producer", "consumer"])
        harness.configure_fetcher_for(persona)
        transport = ASGITransport(app=harness.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Drive JIT materialisation before the test body runs.
            with patch_validator_for_actor(persona):
                await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))
            yield harness, persona, client


@pytest.mark.asyncio
async def test_rest_dependents_endpoint_200(pg_container: str, chain_setup: dict) -> None:
    """GET /v1/capabilities/{E}/dependents returns 200 with valid TraversalResultResponse."""
    slug = chain_setup["slug"]
    ids = chain_setup["ids"]

    async with EntitlementAuthHarness(pg_container) as harness:
        persona = harness.add_persona(slug, roles=["producer", "consumer"])
        harness.configure_fetcher_for(persona)
        transport = ASGITransport(app=harness.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            with patch_validator_for_actor(persona):
                resp = await client.get(
                    f"/v1/capabilities/{ids['E']}/dependents",
                    params={"depth": 5, "edge_types": "depends_on"},
                    headers=bearer_headers(tenant_slug=slug),
                )

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()

    assert body["direction"] == "reverse"
    assert body["cache_hit"] is False
    assert body["root_entity_id"] == str(ids["E"])
    assert body["depth"] == 5

    node_ids = {n["entity_id"] for n in body["nodes"]}
    assert str(ids["D"]) in node_ids
    assert str(ids["C"]) in node_ids
    assert str(ids["B"]) in node_ids
    assert str(ids["A"]) in node_ids
    assert str(ids["E"]) not in node_ids


@pytest.mark.asyncio
async def test_rest_dependents_depth_param(pg_container: str, chain_setup: dict) -> None:
    """depth=1 from E via REST returns only D."""
    slug = chain_setup["slug"]
    ids = chain_setup["ids"]

    async with EntitlementAuthHarness(pg_container) as harness:
        persona = harness.add_persona(slug, roles=["producer", "consumer"])
        harness.configure_fetcher_for(persona)
        transport = ASGITransport(app=harness.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            with patch_validator_for_actor(persona):
                resp = await client.get(
                    f"/v1/capabilities/{ids['E']}/dependents",
                    params={"depth": 1, "edge_types": "depends_on"},
                    headers=bearer_headers(tenant_slug=slug),
                )

    assert resp.status_code == 200, resp.text
    body = resp.json()

    node_ids = {n["entity_id"] for n in body["nodes"]}
    assert str(ids["D"]) in node_ids
    assert str(ids["C"]) not in node_ids


@pytest.mark.asyncio
async def test_rest_dependents_invalid_as_of_returns_422(pg_container: str, chain_setup: dict) -> None:
    """Naive (timezone-unaware) as_of string returns HTTP 422."""
    slug = chain_setup["slug"]
    ids = chain_setup["ids"]

    async with EntitlementAuthHarness(pg_container) as harness:
        persona = harness.add_persona(slug, roles=["producer", "consumer"])
        harness.configure_fetcher_for(persona)
        transport = ASGITransport(app=harness.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            with patch_validator_for_actor(persona):
                resp = await client.get(
                    f"/v1/capabilities/{ids['E']}/dependents",
                    params={"as_of": "2026-01-01T12:00:00"},  # no timezone
                    headers=bearer_headers(tenant_slug=slug),
                )

    assert resp.status_code == 422, f"expected 422 for naive as_of, got {resp.status_code}"


@pytest.mark.asyncio
async def test_rest_dependents_leaf_node_returns_empty_nodes(pg_container: str, chain_setup: dict) -> None:
    """A has no inbound edges; REST endpoint returns empty nodes list."""
    slug = chain_setup["slug"]
    ids = chain_setup["ids"]

    async with EntitlementAuthHarness(pg_container) as harness:
        persona = harness.add_persona(slug, roles=["producer", "consumer"])
        harness.configure_fetcher_for(persona)
        transport = ASGITransport(app=harness.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            with patch_validator_for_actor(persona):
                resp = await client.get(
                    f"/v1/capabilities/{ids['A']}/dependents",
                    params={"depth": 5, "edge_types": "depends_on"},
                    headers=bearer_headers(tenant_slug=slug),
                )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["nodes"] == []
