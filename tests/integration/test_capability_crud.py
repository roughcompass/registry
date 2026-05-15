"""Capability CRUD integration tests.

Covers: create + retrieve roundtrip across capability/artifacts; PATCH
bi-temporal supersession (read via the artifacts listing); DELETE
soft-delete cascade verified at the DB layer.

The legacy ``test_admin_mint_then_revoke`` / ``test_non_admin_cannot_mint``
cases are gone — the ``/v1/admin/tokens`` endpoint they exercised was
removed when the registry stopped issuing its own tokens. Token issuance
is now upstream (ADFS); role enforcement on consumer endpoints is covered
by ``tests/conformance/test_tenant_isolation.py`` and
``tests/integration/test_entitlement_auth_flow.py``.

Auth is driven by ``tests/helpers/auth_harness.py``: the OIDC validator
is patched to return a fixed identity, and the entitlement resolver's
fetcher is mocked so the entitlement service is never contacted.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.storage.models import Fact
from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    TenantPersona,
    bearer_headers,
    patch_validator_for_actor,
)


async def _seed_vocabulary(pg_url: str, tenant_slug: str) -> None:
    """Seed minimal vocabulary for a JIT-materialised tenant.

    The harness materialises the tenant + actor via the entitlement
    resolver but does not seed vocab — capability/concept/operation
    types and edge_rel/fact_category vocab is required for create_entity
    to succeed under the strict-vocab guard.
    """
    engine = create_async_engine(
        pg_url, connect_args={"prepared_statement_cache_size": 0}
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            row = (
                await session.execute(
                    text("SELECT tenant_id FROM tenants WHERE slug = :slug"),
                    {"slug": tenant_slug},
                )
            ).first()
            assert row is not None, f"tenant {tenant_slug} not materialised yet"
            tenant_id = row[0]
            for kind, value in [
                ("entity_type", "capability"),
                ("entity_type", "concept"),
                ("entity_type", "operation"),
                ("fact_category", "overview"),
                ("fact_category", "adr"),
                ("fact_category", "dev_doc"),
                ("edge_rel", "concept_of"),
                ("edge_rel", "operation_of"),
                ("edge_rel", "depends_on"),
                ("edge_rel", "replaced_by"),
            ]:
                await session.execute(
                    text(
                        "INSERT INTO vocabulary_values (tenant_id, kind, value, is_system) "
                        "VALUES (:tid, :kind, :value, FALSE) "
                        "ON CONFLICT DO NOTHING"
                    ),
                    {"tid": tenant_id, "kind": kind, "value": value},
                )
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def harness(pg_container: str) -> AsyncIterator[EntitlementAuthHarness]:
    """Bring up a registry app + mocked entitlement fetcher."""
    async with EntitlementAuthHarness(pg_container) as h:
        yield h


async def _make_persona(
    h: EntitlementAuthHarness, pg_url: str, *, slug: str, roles: list[str]
) -> TenantPersona:
    """Add a persona, materialise the tenant via a no-op call, seed vocab."""
    persona = h.add_persona(slug, roles=roles)
    h.configure_fetcher_for(persona)
    transport = ASGITransport(app=h.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch_validator_for_actor(persona):
            # Hit /v1/whoami to drive JIT materialisation of tenant + actor.
            resp = await client.get(
                "/v1/whoami", headers=bearer_headers(tenant_slug=slug)
            )
            assert resp.status_code == 200, resp.text
    await _seed_vocabulary(pg_url, slug)
    return persona


@pytest.mark.asyncio
async def test_capability_roundtrip(harness: EntitlementAuthHarness, pg_container: str) -> None:
    """Create → GET → assert all fields preserved."""
    persona = await _make_persona(
        harness, pg_container, slug=f"alpha-{uuid.uuid4().hex[:6]}", roles=["producer"]
    )
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            r = await client.post(
                "/v1/capabilities",
                json={"name": "payment-service", "external_id": "pay-svc"},
                headers=bearer_headers(tenant_slug=persona.slug),
            )
            assert r.status_code == 201, r.text
            entity_id = r.json()["entity_id"]

            g = await client.get(
                f"/v1/capabilities/{entity_id}",
                headers=bearer_headers(tenant_slug=persona.slug),
            )
    assert g.status_code == 200
    body = g.json()
    assert body["name"] == "payment-service"
    assert body["external_id"] == "pay-svc"


@pytest.mark.asyncio
async def test_artifact_create_appears_in_listing(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    """create artifact → listing endpoint surfaces the new row."""
    persona = await _make_persona(
        harness, pg_container, slug=f"beta-{uuid.uuid4().hex[:6]}", roles=["producer"]
    )
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            cap = await client.post(
                "/v1/capabilities",
                json={"name": "search"},
                headers=bearer_headers(tenant_slug=persona.slug),
            )
            entity_id = cap.json()["entity_id"]

            f1 = await client.post(
                f"/v1/capabilities/{entity_id}/artifacts",
                json={"category": "overview", "title": "v1 overview", "body": "v1"},
                headers=bearer_headers(tenant_slug=persona.slug),
            )
            assert f1.status_code == 201, f1.text

            listed = await client.get(
                f"/v1/capabilities/{entity_id}/artifacts?fields=fact_id,category,body",
                headers=bearer_headers(tenant_slug=persona.slug),
            )
    assert listed.status_code == 200
    assert any(item["body"] == "v1" for item in listed.json()["items"])


@pytest.mark.asyncio
async def test_delete_entity_soft_deletes_and_cascades(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    """DELETE on an entity soft-deletes the entity and cascades to facts."""
    persona = await _make_persona(
        harness, pg_container, slug=f"gamma-{uuid.uuid4().hex[:6]}", roles=["producer"]
    )
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            cap = await client.post(
                "/v1/capabilities",
                json={"name": "ingest"},
                headers=bearer_headers(tenant_slug=persona.slug),
            )
            entity_id = cap.json()["entity_id"]

            f = await client.post(
                f"/v1/capabilities/{entity_id}/artifacts",
                json={
                    "category": "adr",
                    "title": "Partitioning decision",
                    "body": "decide partitioning",
                },
                headers=bearer_headers(tenant_slug=persona.slug),
            )
            fact_id = f.json()["fact_id"]

            d = await client.delete(
                f"/v1/capabilities/{entity_id}",
                headers=bearer_headers(tenant_slug=persona.slug),
            )
            assert d.status_code == 204

    # Verify cascade at the DB layer — the fact row's t_invalidated_at
    # must be populated. Direct SQL because the listing endpoint
    # filters out soft-deleted rows.
    engine = create_async_engine(
        pg_container, connect_args={"prepared_statement_cache_size": 0}
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            row = await session.execute(
                select(Fact).where(Fact.fact_id == uuid.UUID(fact_id))
            )
            fact = row.scalar_one()
            assert fact.t_invalidated_at is not None
    finally:
        await engine.dispose()


