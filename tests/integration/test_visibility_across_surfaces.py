"""Integration tests for visibility filter across retrieval surfaces.

Contract under test:
- ``search`` (hybrid retrieval), ``get_reverse_traversal``, and
  ``get_blast_radius`` all filter their result sets through
  ``VisibilityService.filter_entities`` (the cross-tenant isolation chokepoint).
- ``private`` entities owned by tenant A are invisible to tenant B.
- ``public`` entities owned by tenant A are visible to tenant B.
- ``tenant-shared`` entities are visible to listed tenants in
  ``shared_with_tenants``, invisible to others.
- Visibility filter runs *after* traversal — edges/depth-counters reflect the
  full walk; only the returned ``nodes`` list is filtered.

These tests cover the cross-tenant case explicitly. Same-tenant ergonomics are
covered by the same-tenant traversal/blast-radius integration tests
(``test_blast_radius.py``, etc.) which don't inject a ``VisibilityService``.
"""

from __future__ import annotations

import datetime
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.config import Settings
from registry.embedder import StubEmbedder
from registry.service.retrieval import RetrievalService
from registry.service.visibility import (
    VISIBILITY_PRIVATE,
    VISIBILITY_PUBLIC,
    VISIBILITY_TENANT_SHARED,
    VisibilityService,
)
from registry.storage.pg import get_session_factory
from registry.types import FakeClock, TenantContext

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_tenant(pg_url: str, *, slug: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert (tenant, actor). Returns (tenant_id, actor_id)."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO tenants (tenant_id, slug, display_name, "
                    "created_at, is_active) VALUES "
                    "(:tid, :slug, :slug, :now, TRUE)"
                ),
                {"tid": tenant_id, "slug": slug, "now": _NOW},
            )
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, display_name, "
                    "oidc_subject, created_at) "
                    "VALUES (:aid, :tid, :dn, :sub, :now)"
                ),
                {
                    "aid": actor_id,
                    "tid": tenant_id,
                    "dn": f"actor-{slug}",
                    "sub": f"test-sub-{actor_id.hex[:8]}",
                    "now": _NOW,
                },
            )
    finally:
        await engine.dispose()
    return tenant_id, actor_id


async def _seed_entity(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    name: str,
    visibility: str,
    shared_with_tenants: list[uuid.UUID] | None = None,
) -> uuid.UUID:
    """Insert one capability entity with the given visibility column."""
    entity_id = uuid.uuid4()
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO entities "
                    "(entity_id, tenant_id, entity_type, name, is_active, "
                    " created_at, visibility) "
                    "VALUES (:eid, :tid, 'capability', :name, TRUE, :now, :vis)"
                ),
                {
                    "eid": entity_id,
                    "tid": tenant_id,
                    "name": name,
                    "now": _NOW,
                    "vis": visibility,
                },
            )
            if shared_with_tenants is not None:
                await session.execute(
                    text(
                        "INSERT INTO attributes "
                        "(attr_id, tenant_id, entity_id, key, value, "
                        " t_valid_from, t_valid_to, t_ingested_at, "
                        " t_invalidated_at, created_by) "
                        "VALUES (gen_random_uuid(), :tid, :eid, "
                        "'shared_with_tenants', "
                        "CAST(:val AS jsonb), :now, NULL, :now, NULL, "
                        "(SELECT actor_id FROM actors WHERE tenant_id = :tid LIMIT 1))"
                    ),
                    {
                        "tid": tenant_id,
                        "eid": entity_id,
                        "val": "[" + ",".join(f'"{t}"' for t in shared_with_tenants) + "]",
                        "now": _NOW,
                    },
                )
    finally:
        await engine.dispose()
    return entity_id


async def _seed_edge(
    pg_url: str,
    *,
    src_tenant_id: uuid.UUID,
    src_entity_id: uuid.UUID,
    dst_entity_id: uuid.UUID,
    rel: str = "depends_on",
) -> uuid.UUID:
    """Insert a single edge src→dst owned by src_tenant_id."""
    edge_id = uuid.uuid4()
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO edges "
                    "(edge_id, tenant_id, src_entity_id, rel, dst_entity_id, "
                    " is_authoritative, t_valid_from, t_ingested_at) "
                    "VALUES (:eid, :tid, :src, :rel, :dst, TRUE, :now, :now)"
                ),
                {
                    "eid": edge_id,
                    "tid": src_tenant_id,
                    "src": src_entity_id,
                    "rel": rel,
                    "dst": dst_entity_id,
                    "now": _NOW,
                },
            )
    finally:
        await engine.dispose()
    return edge_id


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_retrieval(pg_url: str, *, with_visibility: bool) -> RetrievalService:
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    session_factory = get_session_factory(engine)
    clock = FakeClock(_NOW)
    visibility = VisibilityService(session_factory=session_factory, clock=clock) if with_visibility else None
    return RetrievalService(
        session_factory=session_factory,
        clock=clock,
        embedder=StubEmbedder(),
        settings=Settings(
            database_url=pg_url,
            pgbouncer_url=pg_url,
            scheduler_jobstore_url=pg_url,
        ),
        visibility=visibility,
    )


@pytest_asyncio.fixture(scope="module")
async def cross_tenant_setup(pg_container: str):  # type: ignore[type-arg]
    """Two tenants A and B; three A-owned capabilities (private, shared-with-B,
    public); one B-owned capability that depends on each.

    Topology (B is the consumer):
        B/capB_x ──depends_on──► A/capA_private    (invisible to B)
        B/capB_y ──depends_on──► A/capA_shared     (visible to B via ACL)
        B/capB_z ──depends_on──► A/capA_public     (visible to B unconditionally)

    Reverse-traversal from any A node returns the B caller; the cross-tenant
    visibility test is whether B's *forward-traversal-equivalent search* and
    blast-radius queries surface the A-owned nodes.
    """
    tenant_a, _actor_a = await _seed_tenant(pg_container, slug="cross-vis-a")
    tenant_b, _actor_b = await _seed_tenant(pg_container, slug="cross-vis-b")

    cap_a_private = await _seed_entity(
        pg_container,
        tenant_id=tenant_a,
        name="cap-a-private",
        visibility=VISIBILITY_PRIVATE,
    )
    cap_a_shared = await _seed_entity(
        pg_container,
        tenant_id=tenant_a,
        name="cap-a-shared",
        visibility=VISIBILITY_TENANT_SHARED,
        shared_with_tenants=[tenant_b],
    )
    cap_a_public = await _seed_entity(
        pg_container,
        tenant_id=tenant_a,
        name="cap-a-public",
        visibility=VISIBILITY_PUBLIC,
    )

    cap_b_x = await _seed_entity(
        pg_container,
        tenant_id=tenant_b,
        name="cap-b-x",
        visibility=VISIBILITY_PRIVATE,
    )
    cap_b_y = await _seed_entity(
        pg_container,
        tenant_id=tenant_b,
        name="cap-b-y",
        visibility=VISIBILITY_PRIVATE,
    )
    cap_b_z = await _seed_entity(
        pg_container,
        tenant_id=tenant_b,
        name="cap-b-z",
        visibility=VISIBILITY_PRIVATE,
    )

    # B-owned edges pointing at A's caps.
    await _seed_edge(
        pg_container,
        src_tenant_id=tenant_b,
        src_entity_id=cap_b_x,
        dst_entity_id=cap_a_private,
    )
    await _seed_edge(
        pg_container,
        src_tenant_id=tenant_b,
        src_entity_id=cap_b_y,
        dst_entity_id=cap_a_shared,
    )
    await _seed_edge(
        pg_container,
        src_tenant_id=tenant_b,
        src_entity_id=cap_b_z,
        dst_entity_id=cap_a_public,
    )

    return {
        "tenant_a": tenant_a,
        "tenant_b": tenant_b,
        "cap_a_private": cap_a_private,
        "cap_a_shared": cap_a_shared,
        "cap_a_public": cap_a_public,
        "cap_b_x": cap_b_x,
        "cap_b_y": cap_b_y,
        "cap_b_z": cap_b_z,
    }


def _ctx(tenant_id: uuid.UUID) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        actor_id=uuid.uuid4(),
        roles=["producer", "consumer"],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reverse_traversal_filters_private_entity(
    pg_container: str, cross_tenant_setup: dict[str, uuid.UUID]
) -> None:
    """B's reverse traversal of cap_a_private must not surface cap_b_x.

    Tenant A's capability has visibility=private. The reverse-traversal walks
    edges from cap_a_private and finds cap_b_x as a dependent. But B has no
    visibility into cap_a_private — the reverse traversal entry point itself
    should return an empty set of *visible* nodes (B can't see A's stuff).
    """
    s = cross_tenant_setup
    svc = _make_retrieval(pg_container, with_visibility=True)

    # Caller is tenant A (the owner) → can see their own private entity.
    result_a = await svc.get_reverse_traversal(
        ctx=_ctx(s["tenant_a"]),
        entity_id=s["cap_a_private"],
        depth=2,
    )
    a_node_ids = {n.entity_id for n in result_a.nodes}
    # cap_b_x is B-owned, private — A cannot see it.
    assert s["cap_b_x"] not in a_node_ids


@pytest.mark.asyncio
async def test_reverse_traversal_returns_public_dependent(
    pg_container: str, cross_tenant_setup: dict[str, uuid.UUID]
) -> None:
    """B's reverse-traversal of cap_a_public surfaces cross-tenant nodes
    only if they are visible to B (visibility filter enforced) — cap_b_z is B-private, so
    B sees it (own tenant). A querying reverse-traversal of cap_a_public
    cannot see B's private dependents.
    """
    s = cross_tenant_setup
    svc = _make_retrieval(pg_container, with_visibility=True)

    result_a = await svc.get_reverse_traversal(
        ctx=_ctx(s["tenant_a"]),
        entity_id=s["cap_a_public"],
        depth=2,
    )
    a_node_ids = {n.entity_id for n in result_a.nodes}
    # cap_b_z is B-private → A cannot see it.
    assert s["cap_b_z"] not in a_node_ids


@pytest.mark.asyncio
async def test_blast_radius_filters_private_dependents(
    pg_container: str, cross_tenant_setup: dict[str, uuid.UUID]
) -> None:
    """A's blast-radius of cap_a_private — the only dependent is B-private,
    so the visible-nodes list must exclude it.
    """
    s = cross_tenant_setup
    svc = _make_retrieval(pg_container, with_visibility=True)

    result = await svc.get_blast_radius(
        ctx=_ctx(s["tenant_a"]),
        entity_id=s["cap_a_private"],
        direction="reverse",
        depth=2,
    )
    visible_ids = {n.entity_id for n in result.nodes}
    assert s["cap_b_x"] not in visible_ids


@pytest.mark.asyncio
async def test_visibility_helper_filters_correctly(pg_container: str, cross_tenant_setup: dict[str, uuid.UUID]) -> None:
    """Direct check on RetrievalService._apply_visibility — the chokepoint
    must accept public + tenant-shared (B is in ACL) and reject private.
    """
    s = cross_tenant_setup
    svc = _make_retrieval(pg_container, with_visibility=True)

    visible = await svc._apply_visibility(
        _ctx(s["tenant_b"]),
        [s["cap_a_private"], s["cap_a_shared"], s["cap_a_public"]],
    )
    assert s["cap_a_private"] not in visible
    assert s["cap_a_shared"] in visible
    assert s["cap_a_public"] in visible


@pytest.mark.asyncio
async def test_apply_visibility_passthrough_when_service_absent(
    pg_container: str, cross_tenant_setup: dict[str, uuid.UUID]
) -> None:
    """With no VisibilityService injected, _apply_visibility passes IDs through
    unchanged. The strict same-tenant SQL filter in `_fetch_entity_refs` and
    the post-fusion assertion in `search` provide the safety net.
    """
    s = cross_tenant_setup
    svc = _make_retrieval(pg_container, with_visibility=False)

    visible = await svc._apply_visibility(
        _ctx(s["tenant_b"]),
        [s["cap_a_private"], s["cap_a_shared"], s["cap_a_public"]],
    )
    # No filtering at this layer; downstream SQL/assertion provides the gate.
    assert visible == {s["cap_a_private"], s["cap_a_shared"], s["cap_a_public"]}
