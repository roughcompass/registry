"""Pair-discoverability integration tests.

Scenarios:
1. Create one integration ``I`` with ``composes(I, A)`` and ``composes(I, B)``.
   The trigger populates two ``integration_pairs`` rows.
   ``GET /v1/integrations?connects=A&and=B`` returns ``[I]``.
2. Visibility chokepoint: the same integration with visibility=private
   owned by Tenant X is invisible to Tenant Y; the lookup returns [].
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.service.visibility import (
    VISIBILITY_PRIVATE,
    VISIBILITY_PUBLIC,
)
from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    TenantPersona,
    bearer_headers,
    patch_validator_for_actor,
)

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_entity(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    entity_type: str,
    name: str,
    visibility: str = VISIBILITY_PUBLIC,
) -> uuid.UUID:
    eid = uuid.uuid4()
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO entities "
                    "(entity_id, tenant_id, entity_type, name, is_active, "
                    " created_at, visibility) "
                    "VALUES (:eid, :tid, :etype, :name, TRUE, :now, :vis)"
                ),
                {
                    "eid": eid,
                    "tid": tenant_id,
                    "etype": entity_type,
                    "name": name,
                    "now": _NOW,
                    "vis": visibility,
                },
            )
    finally:
        await engine.dispose()
    return eid


async def _composes(pg_url: str, tenant_id: uuid.UUID, src: uuid.UUID, dst: uuid.UUID) -> None:
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO edges "
                    "(edge_id, tenant_id, src_entity_id, rel, dst_entity_id, "
                    " properties, t_valid_from, t_ingested_at) "
                    "VALUES (gen_random_uuid(), :tid, :src, 'composes', :dst, "
                    "        NULL, :now, :now)"
                ),
                {"tid": tenant_id, "src": src, "dst": dst, "now": _NOW},
            )
    finally:
        await engine.dispose()


async def _get_tenant_id(pg_url: str, slug: str) -> uuid.UUID:
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            row = (
                await session.execute(
                    text("SELECT tenant_id FROM tenants WHERE slug = :slug"), {"slug": slug}
                )
            ).first()
            assert row is not None, f"tenant {slug} not found"
            return uuid.UUID(str(row[0]))
    finally:
        await engine.dispose()


async def _make_persona(
    h: EntitlementAuthHarness,
    pg_url: str,
    *,
    slug: str,
    roles: list[str],
) -> TenantPersona:
    """Materialise tenant + actor via /v1/whoami."""
    persona = h.add_persona(slug, roles=roles)
    h.configure_fetcher_for(persona)
    transport = ASGITransport(app=h.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch_validator_for_actor(persona):
            resp = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))
            assert resp.status_code == 200, resp.text
    return persona


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def app_client(pg_container: str) -> AsyncIterator[tuple[AsyncClient, EntitlementAuthHarness]]:
    async with EntitlementAuthHarness(pg_container) as harness:
        transport = ASGITransport(app=harness.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, harness


# ---------------------------------------------------------------------------
# Scenario 1 — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pair_lookup_returns_integration_connecting_two_caps(
    pg_container: str, app_client: tuple[AsyncClient, EntitlementAuthHarness]
) -> None:
    client, harness = app_client
    slug = f"intpairs-h-{uuid.uuid4().hex[:6]}"
    persona = await _make_persona(harness, pg_container, slug=slug, roles=["consumer", "producer"])
    tid = await _get_tenant_id(pg_container, slug)

    cap_a = await _seed_entity(pg_container, tenant_id=tid, entity_type="capability", name="cap-a")
    cap_b = await _seed_entity(pg_container, tenant_id=tid, entity_type="capability", name="cap-b")
    integration = await _seed_entity(pg_container, tenant_id=tid, entity_type="integration", name="adapter-ab")
    await _composes(pg_container, tid, integration, cap_a)
    await _composes(pg_container, tid, integration, cap_b)

    # Trigger should have populated 2 rows in integration_pairs.
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            res = await session.execute(
                text("SELECT COUNT(*) FROM integration_pairs WHERE integration_entity_id = :i"),
                {"i": integration},
            )
            assert (res.scalar() or 0) == 2
    finally:
        await engine.dispose()

    harness.configure_fetcher_for(persona)
    with patch_validator_for_actor(persona):
        resp = await client.get(
            "/v1/integrations",
            headers=bearer_headers(tenant_slug=persona.slug),
            params={"connects": str(cap_a), "and": str(cap_b)},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    items = body["items"] if isinstance(body, dict) else body
    assert len(items) == 1
    assert items[0]["entity_id"] == str(integration)
    assert items[0]["entity_type"] == "integration"


# ---------------------------------------------------------------------------
# Scenario 2 — visibility filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_private_integration_is_invisible_to_other_tenants(
    pg_container: str, app_client: tuple[AsyncClient, EntitlementAuthHarness]
) -> None:
    client, harness = app_client

    slug_a = f"intpairs-priv-a-{uuid.uuid4().hex[:6]}"
    slug_b = f"intpairs-priv-b-{uuid.uuid4().hex[:6]}"
    _persona_a = await _make_persona(harness, pg_container, slug=slug_a, roles=["producer"])
    persona_b = await _make_persona(harness, pg_container, slug=slug_b, roles=["consumer"])
    a_tid = await _get_tenant_id(pg_container, slug_a)

    cap_a = await _seed_entity(pg_container, tenant_id=a_tid, entity_type="capability", name="A")
    cap_b = await _seed_entity(pg_container, tenant_id=a_tid, entity_type="capability", name="B")
    integration = await _seed_entity(
        pg_container,
        tenant_id=a_tid,
        entity_type="integration",
        name="private-integration",
        visibility=VISIBILITY_PRIVATE,
    )
    await _composes(pg_container, a_tid, integration, cap_a)
    await _composes(pg_container, a_tid, integration, cap_b)

    harness.configure_fetcher_for(persona_b)
    with patch_validator_for_actor(persona_b):
        resp = await client.get(
            "/v1/integrations",
            headers=bearer_headers(tenant_slug=persona_b.slug),
            params={"connects": str(cap_a), "and": str(cap_b)},
        )
    assert resp.status_code == 200
    body = resp.json()
    items = body["items"] if isinstance(body, dict) else body
    assert items == []
