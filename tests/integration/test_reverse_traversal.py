"""Integration tests for reverse traversal service + REST endpoint.

Contract under test
-----------------------------------------------------------------
Five-capability chain:  A → B → C → D → E  (edge rel: depends_on)

Forward from A  (get_dependencies): B(1), C(2), D(3), E(4)
Reverse from E  (get_reverse_traversal): D(1), C(2), B(3), A(4)

Both directions must return symmetric sets of nodes (same entities, mirrored
depths).  ``cache_hit`` must be ``False`` when the closure cache is not warmed.
``version_satisfied`` must be ``True`` for every edge when no version predicates
are configured.

The REST endpoint ``GET /v1/capabilities/{entity_id}/dependents`` must return
HTTP 200 with a ``TraversalResultResponse`` body that matches the service result.

Visibility: same-tenant tests — all nodes belong to the calling tenant, so all
are returned.
"""

from __future__ import annotations

import datetime
import secrets
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.api.auth.tokens import hash_token
from registry.config import Settings
from registry.embedder import StubEmbedder
from registry.main import create_app
from registry.service.retrieval import RetrievalService
from registry.storage.pg import get_session_factory
from registry.types import FakeClock, TemporalFilter, TenantContext, TraversalResult

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_tenant(pg_url: str, *, slug: str) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Insert tenant + actor + producer/consumer API token.

    Returns (tenant_id, actor_id, raw_token).
    """
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


async def _seed_five_cap_chain(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
) -> dict[str, uuid.UUID]:
    """Insert entities A→B→C→D→E and depends_on edges.

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

            # Insert edges: A→B, B→C, C→D, D→E
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
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def chain_setup(pg_container: str):  # type: ignore[type-arg]
    """Seed tenant + 5-cap chain once for the whole module.

    Returns dict with:
      tenant_id, actor_id, raw_token, ids (entity/edge UUIDs by label).
    """
    tenant_id, actor_id, raw_token = await _seed_tenant(pg_container, slug=f"t05-{uuid.uuid4().hex[:8]}")
    chain_ids = await _seed_five_cap_chain(pg_container, tenant_id=tenant_id)
    return {
        "tenant_id": tenant_id,
        "actor_id": actor_id,
        "raw_token": raw_token,
        "ids": chain_ids,
        "pg_url": pg_container,
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
    """Reverse traversal from E in A→B→C→D→E must return D, C, B, A as nodes."""
    setup = chain_setup
    svc = _make_retrieval_service(setup["pg_url"])
    ctx = TenantContext(
        tenant_id=setup["tenant_id"],
        actor_id=setup["actor_id"],
        roles=["consumer"],
    )
    ids = setup["ids"]

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
    setup = chain_setup
    svc = _make_retrieval_service(setup["pg_url"])
    ctx = TenantContext(
        tenant_id=setup["tenant_id"],
        actor_id=setup["actor_id"],
        roles=["consumer"],
    )
    ids = setup["ids"]

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
    setup = chain_setup
    svc = _make_retrieval_service(setup["pg_url"])
    ctx = TenantContext(
        tenant_id=setup["tenant_id"],
        actor_id=setup["actor_id"],
        roles=["consumer"],
    )
    ids = setup["ids"]

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
    setup = chain_setup
    svc = _make_retrieval_service(setup["pg_url"])
    ctx = TenantContext(
        tenant_id=setup["tenant_id"],
        actor_id=setup["actor_id"],
        roles=["consumer"],
    )
    ids = setup["ids"]

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
    setup = chain_setup
    svc = _make_retrieval_service(setup["pg_url"])
    ctx = TenantContext(
        tenant_id=setup["tenant_id"],
        actor_id=setup["actor_id"],
        roles=["consumer"],
    )
    ids = setup["ids"]

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
    setup = chain_setup
    svc = _make_retrieval_service(setup["pg_url"])
    ctx = TenantContext(
        tenant_id=setup["tenant_id"],
        actor_id=setup["actor_id"],
        roles=["consumer"],
    )
    ids = setup["ids"]

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
    setup = chain_setup
    svc = _make_retrieval_service(setup["pg_url"])
    ctx = TenantContext(
        tenant_id=setup["tenant_id"],
        actor_id=setup["actor_id"],
        roles=["consumer"],
    )
    # A has no inbound edges in the chain (it depends on nothing upstream)
    ids = setup["ids"]

    result = await svc.get_reverse_traversal(
        ctx=ctx,
        entity_id=ids["A"],
        depth=5,
        edge_types=["depends_on"],
    )

    assert result.nodes == [], f"A has no inbound depends_on edges; expected empty nodes, got {result.nodes}"
    assert result.cache_hit is False


# ---------------------------------------------------------------------------
# REST endpoint tests (HTTP via httpx AsyncClient)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def http_client(chain_setup: dict):  # type: ignore[type-arg]
    """Spin up the FastAPI app and return an httpx AsyncClient."""
    pg_url = chain_setup["pg_url"]
    settings = Settings(
        database_url=pg_url,
        pgbouncer_url=pg_url,
        scheduler_jobstore_url=pg_url,
        embedding_model="stub",
        scheduler_use_memory_jobstore=True,
    )
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, chain_setup["raw_token"], chain_setup["ids"]


@pytest.mark.asyncio
async def test_rest_dependents_endpoint_200(http_client) -> None:
    """GET /v1/capabilities/{E}/dependents returns 200 with valid TraversalResultResponse."""
    client, raw_token, ids = http_client

    resp = await client.get(
        f"/v1/capabilities/{ids['E']}/dependents",
        params={"depth": 5, "edge_types": "depends_on"},
        headers={"Authorization": f"Bearer {raw_token}"},
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
async def test_rest_dependents_depth_param(http_client) -> None:
    """depth=1 from E via REST returns only D."""
    client, raw_token, ids = http_client

    resp = await client.get(
        f"/v1/capabilities/{ids['E']}/dependents",
        params={"depth": 1, "edge_types": "depends_on"},
        headers={"Authorization": f"Bearer {raw_token}"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()

    node_ids = {n["entity_id"] for n in body["nodes"]}
    assert str(ids["D"]) in node_ids
    assert str(ids["C"]) not in node_ids


@pytest.mark.asyncio
async def test_rest_dependents_invalid_as_of_returns_422(http_client) -> None:
    """Naive (timezone-unaware) as_of string returns HTTP 422."""
    client, raw_token, ids = http_client

    resp = await client.get(
        f"/v1/capabilities/{ids['E']}/dependents",
        params={"as_of": "2026-01-01T12:00:00"},  # no timezone
        headers={"Authorization": f"Bearer {raw_token}"},
    )

    assert resp.status_code == 422, f"expected 422 for naive as_of, got {resp.status_code}"


@pytest.mark.asyncio
async def test_rest_dependents_leaf_node_returns_empty_nodes(http_client) -> None:
    """A has no inbound edges; REST endpoint returns empty nodes list."""
    client, raw_token, ids = http_client

    resp = await client.get(
        f"/v1/capabilities/{ids['A']}/dependents",
        params={"depth": 5, "edge_types": "depends_on"},
        headers={"Authorization": f"Bearer {raw_token}"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["nodes"] == []
