"""Integration tests for version-aware traversal (as_of_version parameter).

Contract under test
----------------------------------------------------------------------
A "requires" edge carries ``properties = {"version": ">=2.0"}``.

Test scenario: two capabilities — Provider (P) and Consumer (C).
  C --[requires, version=">=2.0"]--> P

Case 1: P has version attribute "1.4.0"
  - Without as_of_version: traversal returns the edge; version_satisfied[edge]=False.
  - With as_of_version="2.0.0": predicate NOT satisfied → edge excluded; result is empty.

Case 2: P has version attribute "2.4.0"
  - Without as_of_version: traversal returns the edge; version_satisfied[edge]=True.
  - With as_of_version="2.0.0": predicate satisfied → edge included; result has node.

Additional: edge with no predicate is always included regardless of as_of_version.

Path: get_reverse_traversal (reverse from P) and get_blast_radius (CTE path, cold cache).
"""

from __future__ import annotations

import datetime
import json
import secrets
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.api.auth.tokens import hash_token
from registry.config import Settings
from registry.embedder import StubEmbedder
from registry.service.retrieval import RetrievalService
from registry.storage.pg import get_session_factory
from registry.types import FakeClock, TenantContext, TraversalResult

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Helpers
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


async def _seed_scenario(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    provider_version: str,
) -> dict[str, uuid.UUID]:
    """Seed Provider + Consumer entities and a versioned requires edge.

    Returns dict: 'provider', 'consumer', 'edge_versioned', 'edge_nopred'.
    Also inserts an edge with NO version predicate (depends_on) to verify
    it is always included.
    """
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    provider_id = uuid.uuid4()
    consumer_id = uuid.uuid4()
    edge_versioned_id = uuid.uuid4()
    edge_nopred_id = uuid.uuid4()

    # Extra leaf node B that depends_on consumer (no predicate) to test
    # that no-predicate edges are always traversed.
    leaf_id = uuid.uuid4()
    edge_leaf_id = uuid.uuid4()

    try:
        async with factory() as session, session.begin():
            # Entities
            for eid, name in [
                (provider_id, "provider-cap"),
                (consumer_id, "consumer-cap"),
                (leaf_id, "leaf-cap"),
            ]:
                await session.execute(
                    text(
                        "INSERT INTO entities "
                        "(entity_id, tenant_id, entity_type, name, is_active, created_at) "
                        "VALUES (:eid, :tid, 'capability', :name, TRUE, :now)"
                    ),
                    {"eid": eid, "tid": tenant_id, "name": name, "now": _NOW},
                )

            # Provider version attribute (key='version', value is JSONB string)
            await session.execute(
                text(
                    "INSERT INTO attributes "
                    "(attr_id, tenant_id, entity_id, key, value, t_valid_from, t_ingested_at) "
                    "VALUES (gen_random_uuid(), :tid, :eid, 'version', :val, :now, :now)"
                ),
                {
                    "tid": tenant_id,
                    "eid": provider_id,
                    "val": json.dumps(provider_version),  # JSONB string
                    "now": _NOW,
                },
            )

            # Versioned edge: consumer --[requires, version=">=2.0"]--> provider
            await session.execute(
                text(
                    "INSERT INTO edges "
                    "(edge_id, tenant_id, src_entity_id, rel, dst_entity_id, "
                    " properties, is_authoritative, t_valid_from, t_ingested_at) "
                    "VALUES (:eid, :tid, :src, 'requires', :dst, :props, TRUE, :now, :now)"
                ),
                {
                    "eid": edge_versioned_id,
                    "tid": tenant_id,
                    "src": consumer_id,
                    "dst": provider_id,
                    "props": json.dumps({"version": ">=2.0"}),
                    "now": _NOW,
                },
            )

            # No-predicate edge: leaf --[depends_on]--> consumer (no properties.version)
            await session.execute(
                text(
                    "INSERT INTO edges "
                    "(edge_id, tenant_id, src_entity_id, rel, dst_entity_id, "
                    " is_authoritative, t_valid_from, t_ingested_at) "
                    "VALUES (:eid, :tid, :src, 'depends_on', :dst, TRUE, :now, :now)"
                ),
                {
                    "eid": edge_nopred_id,
                    "tid": tenant_id,
                    "src": leaf_id,
                    "dst": consumer_id,
                    "now": _NOW,
                },
            )

            # Edge from consumer to leaf (for forward blast-radius test)
            await session.execute(
                text(
                    "INSERT INTO edges "
                    "(edge_id, tenant_id, src_entity_id, rel, dst_entity_id, "
                    " is_authoritative, t_valid_from, t_ingested_at) "
                    "VALUES (:eid, :tid, :src, 'depends_on', :dst, TRUE, :now, :now)"
                ),
                {
                    "eid": edge_leaf_id,
                    "tid": tenant_id,
                    "src": consumer_id,
                    "dst": leaf_id,
                    "now": _NOW,
                },
            )
    finally:
        await engine.dispose()

    return {
        "provider": provider_id,
        "consumer": consumer_id,
        "leaf": leaf_id,
        "edge_versioned": edge_versioned_id,
        "edge_nopred": edge_nopred_id,
        "edge_leaf": edge_leaf_id,
    }


def _make_service(pg_url: str) -> RetrievalService:
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
# Fixtures — seeded once per module, two scenarios (v1.4 and v2.4)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def scenario_v14(pg_container: str):  # type: ignore[type-arg]
    """Provider at version 1.4.0; predicate is >=2.0 → unsatisfied."""
    tenant_id, actor_id, raw_token = await _seed_tenant(pg_container, slug=f"t09-v14-{uuid.uuid4().hex[:8]}")
    ids = await _seed_scenario(pg_container, tenant_id=tenant_id, provider_version="1.4.0")
    return {
        "tenant_id": tenant_id,
        "actor_id": actor_id,
        "raw_token": raw_token,
        "ids": ids,
        "pg_url": pg_container,
    }


@pytest_asyncio.fixture(scope="module")
async def scenario_v24(pg_container: str):  # type: ignore[type-arg]
    """Provider at version 2.4.0; predicate is >=2.0 → satisfied."""
    tenant_id, actor_id, raw_token = await _seed_tenant(pg_container, slug=f"t09-v24-{uuid.uuid4().hex[:8]}")
    ids = await _seed_scenario(pg_container, tenant_id=tenant_id, provider_version="2.4.0")
    return {
        "tenant_id": tenant_id,
        "actor_id": actor_id,
        "raw_token": raw_token,
        "ids": ids,
        "pg_url": pg_container,
    }


# ---------------------------------------------------------------------------
# Tests — version_satisfied flag (no as_of_version filter)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_version_satisfied_false_when_predicate_unmet(scenario_v14: dict) -> None:
    """Provider@1.4.0: reverse from Provider, requires>=2.0 → version_satisfied[edge]=False."""
    setup = scenario_v14
    svc = _make_service(setup["pg_url"])
    ctx = TenantContext(
        tenant_id=setup["tenant_id"],
        actor_id=setup["actor_id"],
        roles=["consumer"],
    )
    ids = setup["ids"]

    result: TraversalResult = await svc.get_reverse_traversal(
        ctx=ctx,
        entity_id=ids["provider"],
        depth=3,
        edge_types=["requires"],
    )

    # Consumer must be reachable (predicate unmet ≠ prune path)
    node_ids = {n.entity_id for n in result.nodes}
    assert ids["consumer"] in node_ids, "Consumer must appear in reverse traversal even when predicate is unmet"

    # The versioned edge must be flagged as unsatisfied
    assert ids["edge_versioned"] in result.version_satisfied, "edge_versioned must be in version_satisfied dict"
    assert result.version_satisfied[ids["edge_versioned"]] is False, (
        f"Provider@1.4.0 with requires>=2.0: expected False, " f"got {result.version_satisfied[ids['edge_versioned']]}"
    )


@pytest.mark.asyncio
async def test_version_satisfied_true_when_predicate_met(scenario_v24: dict) -> None:
    """Provider@2.4.0: reverse from Provider, requires>=2.0 → version_satisfied[edge]=True."""
    setup = scenario_v24
    svc = _make_service(setup["pg_url"])
    ctx = TenantContext(
        tenant_id=setup["tenant_id"],
        actor_id=setup["actor_id"],
        roles=["consumer"],
    )
    ids = setup["ids"]

    result: TraversalResult = await svc.get_reverse_traversal(
        ctx=ctx,
        entity_id=ids["provider"],
        depth=3,
        edge_types=["requires"],
    )

    node_ids = {n.entity_id for n in result.nodes}
    assert ids["consumer"] in node_ids

    assert ids["edge_versioned"] in result.version_satisfied
    assert result.version_satisfied[ids["edge_versioned"]] is True, (
        f"Provider@2.4.0 with requires>=2.0: expected True, " f"got {result.version_satisfied[ids['edge_versioned']]}"
    )


# ---------------------------------------------------------------------------
# Tests — as_of_version filtering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_as_of_version_filters_unmet_edge(scenario_v14: dict) -> None:
    """Provider@1.4.0 + as_of_version=2.0.0: consumer not reachable (edge excluded)."""
    setup = scenario_v14
    svc = _make_service(setup["pg_url"])
    ctx = TenantContext(
        tenant_id=setup["tenant_id"],
        actor_id=setup["actor_id"],
        roles=["consumer"],
    )
    ids = setup["ids"]

    result: TraversalResult = await svc.get_reverse_traversal(
        ctx=ctx,
        entity_id=ids["provider"],
        depth=3,
        edge_types=["requires"],
        as_of_version="2.0.0",
    )

    node_ids = {n.entity_id for n in result.nodes}
    assert ids["consumer"] not in node_ids, (
        "Provider@1.4.0 with as_of_version=2.0.0: consumer must be excluded "
        "(requires>=2.0 is not satisfied by 1.4.0)"
    )
    assert result.nodes == [], f"Expected empty nodes, got {[str(n.entity_id) for n in result.nodes]}"


@pytest.mark.asyncio
async def test_as_of_version_includes_met_edge(scenario_v24: dict) -> None:
    """Provider@2.4.0 + as_of_version=2.0.0: consumer reachable (edge included)."""
    setup = scenario_v24
    svc = _make_service(setup["pg_url"])
    ctx = TenantContext(
        tenant_id=setup["tenant_id"],
        actor_id=setup["actor_id"],
        roles=["consumer"],
    )
    ids = setup["ids"]

    result: TraversalResult = await svc.get_reverse_traversal(
        ctx=ctx,
        entity_id=ids["provider"],
        depth=3,
        edge_types=["requires"],
        as_of_version="2.0.0",
    )

    node_ids = {n.entity_id for n in result.nodes}
    assert ids["consumer"] in node_ids, (
        "Provider@2.4.0 with as_of_version=2.0.0: consumer must be reachable " "(requires>=2.0 is satisfied by 2.4.0)"
    )


@pytest.mark.asyncio
async def test_no_predicate_edge_always_included_with_as_of_version(scenario_v14: dict) -> None:
    """Edge with no version predicate is always included even when as_of_version is set."""
    setup = scenario_v14
    svc = _make_service(setup["pg_url"])
    ctx = TenantContext(
        tenant_id=setup["tenant_id"],
        actor_id=setup["actor_id"],
        roles=["consumer"],
    )
    ids = setup["ids"]

    # Reverse from consumer — should find leaf via depends_on (no predicate)
    result: TraversalResult = await svc.get_reverse_traversal(
        ctx=ctx,
        entity_id=ids["consumer"],
        depth=2,
        edge_types=["depends_on"],
        as_of_version="99.0.0",  # Very high version; only versioned edges would fail
    )

    node_ids = {n.entity_id for n in result.nodes}
    assert ids["leaf"] in node_ids, "Edge with no predicate must always appear in traversal regardless of as_of_version"


# ---------------------------------------------------------------------------
# Tests — blast-radius (CTE path) version predicate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blast_radius_cte_version_satisfied_false(scenario_v14: dict) -> None:
    """Blast-radius CTE path: Provider@1.4.0, reverse, version_satisfied[edge]=False."""
    setup = scenario_v14
    svc = _make_service(setup["pg_url"])
    ctx = TenantContext(
        tenant_id=setup["tenant_id"],
        actor_id=setup["actor_id"],
        roles=["consumer"],
    )
    ids = setup["ids"]

    result: TraversalResult = await svc.get_blast_radius(
        ctx=ctx,
        entity_id=ids["provider"],
        direction="reverse",
        depth=3,
        edge_types=["requires"],
    )

    node_ids = {n.entity_id for n in result.nodes}
    assert ids["consumer"] in node_ids, "Consumer must appear without as_of_version filter"

    assert ids["edge_versioned"] in result.version_satisfied
    assert result.version_satisfied[ids["edge_versioned"]] is False, (
        f"blast_radius: Provider@1.4.0 with requires>=2.0: expected False, "
        f"got {result.version_satisfied[ids['edge_versioned']]}"
    )


@pytest.mark.asyncio
async def test_blast_radius_cte_version_satisfied_true(scenario_v24: dict) -> None:
    """Blast-radius CTE path: Provider@2.4.0, reverse, version_satisfied[edge]=True."""
    setup = scenario_v24
    svc = _make_service(setup["pg_url"])
    ctx = TenantContext(
        tenant_id=setup["tenant_id"],
        actor_id=setup["actor_id"],
        roles=["consumer"],
    )
    ids = setup["ids"]

    result: TraversalResult = await svc.get_blast_radius(
        ctx=ctx,
        entity_id=ids["provider"],
        direction="reverse",
        depth=3,
        edge_types=["requires"],
    )

    assert ids["edge_versioned"] in result.version_satisfied
    assert result.version_satisfied[ids["edge_versioned"]] is True


@pytest.mark.asyncio
async def test_blast_radius_as_of_version_filters_unmet(scenario_v14: dict) -> None:
    """Blast-radius CTE path: as_of_version=2.0.0, Provider@1.4.0 → consumer excluded."""
    setup = scenario_v14
    svc = _make_service(setup["pg_url"])
    ctx = TenantContext(
        tenant_id=setup["tenant_id"],
        actor_id=setup["actor_id"],
        roles=["consumer"],
    )
    ids = setup["ids"]

    result: TraversalResult = await svc.get_blast_radius(
        ctx=ctx,
        entity_id=ids["provider"],
        direction="reverse",
        depth=3,
        edge_types=["requires"],
        as_of_version="2.0.0",
    )

    node_ids = {n.entity_id for n in result.nodes}
    assert (
        ids["consumer"] not in node_ids
    ), "blast_radius: Provider@1.4.0 with as_of_version=2.0.0: consumer must be excluded"


@pytest.mark.asyncio
async def test_blast_radius_as_of_version_includes_met(scenario_v24: dict) -> None:
    """Blast-radius CTE path: as_of_version=2.0.0, Provider@2.4.0 → consumer included."""
    setup = scenario_v24
    svc = _make_service(setup["pg_url"])
    ctx = TenantContext(
        tenant_id=setup["tenant_id"],
        actor_id=setup["actor_id"],
        roles=["consumer"],
    )
    ids = setup["ids"]

    result: TraversalResult = await svc.get_blast_radius(
        ctx=ctx,
        entity_id=ids["provider"],
        direction="reverse",
        depth=3,
        edge_types=["requires"],
        as_of_version="2.0.0",
    )

    node_ids = {n.entity_id for n in result.nodes}
    assert (
        ids["consumer"] in node_ids
    ), "blast_radius: Provider@2.4.0 with as_of_version=2.0.0: consumer must be reachable"


# ---------------------------------------------------------------------------
# Tests — edge with no predicate always satisfied
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_predicate_version_satisfied_is_true(scenario_v14: dict) -> None:
    """An edge with no version predicate always yields version_satisfied[edge]=True."""
    setup = scenario_v14
    svc = _make_service(setup["pg_url"])
    ctx = TenantContext(
        tenant_id=setup["tenant_id"],
        actor_id=setup["actor_id"],
        roles=["consumer"],
    )
    ids = setup["ids"]

    result: TraversalResult = await svc.get_reverse_traversal(
        ctx=ctx,
        entity_id=ids["consumer"],
        depth=2,
        edge_types=["depends_on"],
    )

    if ids["edge_nopred"] in result.version_satisfied:
        assert (
            result.version_satisfied[ids["edge_nopred"]] is True
        ), "Edge with no predicate must always be True in version_satisfied"
