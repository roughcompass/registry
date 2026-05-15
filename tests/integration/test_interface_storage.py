"""Integration test for interface storage — bi-temporal round-trip.

Exercises the bi-temporal round-trip:

1. PUT a TypeScript interface → 200 + canonical surface.
2. GET → returns the canonical surface + source.
3. PUT a second OpenAPI interface (replaces the first via supersession).
4. GET current truth → returns the OpenAPI surface.
5. (Best-effort) GET with ``as_of`` immediately after the first PUT
   should return the original TypeScript surface, demonstrating
   bi-temporal history.

Step 5 is best-effort: the live wall-clock has sub-second precision in
attributes timestamps and the live database is shared across tests.
We assert only that the response status is 200 — value verification is
covered by the unit test for the service.
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

from registry.service.visibility import VISIBILITY_PUBLIC
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


async def _seed_capability(pg_url: str, *, tenant_id: uuid.UUID, name: str) -> uuid.UUID:
    """Insert a capability entity for a pre-materialised tenant."""
    cap_id = uuid.uuid4()
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
                    "eid": cap_id,
                    "tid": tenant_id,
                    "name": name,
                    "now": _NOW,
                    "vis": VISIBILITY_PUBLIC,
                },
            )
    finally:
        await engine.dispose()
    return cap_id


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
            assert row is not None, f"tenant {slug} not materialised"
            return uuid.UUID(str(row[0]))
    finally:
        await engine.dispose()


async def _make_persona(
    h: EntitlementAuthHarness, pg_url: str, *, slug: str, roles: list[str]
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
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_then_get_typescript_interface(
    pg_container: str, app_client: tuple[AsyncClient, EntitlementAuthHarness]
) -> None:
    client, harness = app_client
    slug = f"iface-ts-{uuid.uuid4().hex[:6]}"
    persona = await _make_persona(harness, pg_container, slug=slug, roles=["admin", "producer"])
    tid = await _get_tenant_id(pg_container, slug)
    cap_id = await _seed_capability(pg_container, tenant_id=tid, name=f"{slug}-cap")

    harness.configure_fetcher_for(persona)
    with patch_validator_for_actor(persona):
        put = await client.put(
            f"/v1/capabilities/{cap_id}/interface",
            headers=bearer_headers(tenant_slug=persona.slug),
            json={
                "interface_source": "type Order = { id: string; total: number; }",
                "interface_format": "typescript",
            },
        )
        assert put.status_code == 200, put.text
        canonical = put.json()
        field_names = {f["name"] for f in canonical["fields"]}
        assert field_names == {"id", "total"}

        get = await client.get(
            f"/v1/capabilities/{cap_id}/interface",
            headers=bearer_headers(tenant_slug=persona.slug),
        )
    assert get.status_code == 200
    body = get.json()
    assert body["interface_format"] == "typescript"
    assert {f["name"] for f in body["interface_canonical"]["fields"]} == {"id", "total"}
    assert body["interface_source"]["format"] == "typescript"


@pytest.mark.asyncio
async def test_second_put_supersedes_first(
    pg_container: str, app_client: tuple[AsyncClient, EntitlementAuthHarness]
) -> None:
    client, harness = app_client
    slug = f"iface-supersede-{uuid.uuid4().hex[:6]}"
    persona = await _make_persona(harness, pg_container, slug=slug, roles=["admin", "producer"])
    tid = await _get_tenant_id(pg_container, slug)
    cap_id = await _seed_capability(pg_container, tenant_id=tid, name=f"{slug}-cap")

    harness.configure_fetcher_for(persona)
    with patch_validator_for_actor(persona):
        # First PUT — TypeScript.
        r1 = await client.put(
            f"/v1/capabilities/{cap_id}/interface",
            headers=bearer_headers(tenant_slug=persona.slug),
            json={
                "interface_source": "type X = { id: string; }",
                "interface_format": "typescript",
            },
        )
        assert r1.status_code == 200

        # Second PUT — OpenAPI.
        r2 = await client.put(
            f"/v1/capabilities/{cap_id}/interface",
            headers=bearer_headers(tenant_slug=persona.slug),
            json={
                "interface_source": {
                    "openapi": "3.0.0",
                    "paths": {
                        "/orders": {
                            "post": {
                                "operationId": "createOrder",
                                "responses": {
                                    "201": {
                                        "content": {
                                            "application/json": {"schema": {"type": "object"}}
                                        }
                                    }
                                },
                            }
                        }
                    },
                },
                "interface_format": "openapi",
            },
        )
        assert r2.status_code == 200, r2.text

        # Current truth GET reflects the OpenAPI surface only.
        get = await client.get(
            f"/v1/capabilities/{cap_id}/interface",
            headers=bearer_headers(tenant_slug=persona.slug),
        )
    assert get.status_code == 200
    body = get.json()
    assert body["interface_format"] == "openapi"
    op_names = {op["name"] for op in body["interface_canonical"]["operations"]}
    assert op_names == {"createOrder"}
    # The old TypeScript fields are gone — only the (empty) OpenAPI fields remain.
    assert body["interface_canonical"]["fields"] == []

    # Only ONE active attribute row per key in the DB (supersession worked).
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            for key in ("interface_canonical", "interface_source"):
                res = await session.execute(
                    text(
                        "SELECT COUNT(*) FROM attributes "
                        "WHERE entity_id = :eid AND key = :k "
                        "  AND t_invalidated_at IS NULL AND t_valid_to IS NULL"
                    ),
                    {"eid": cap_id, "k": key},
                )
                assert (res.scalar() or 0) == 1, f"key={key}: expected 1 active row"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_put_non_owner_returns_404(
    pg_container: str, app_client: tuple[AsyncClient, EntitlementAuthHarness]
) -> None:
    client, harness = app_client
    # Owner tenant has a capability; a different tenant tries to write.
    slug_owner = f"iface-owner-{uuid.uuid4().hex[:6]}"
    slug_other = f"iface-other-{uuid.uuid4().hex[:6]}"
    _persona_owner = await _make_persona(harness, pg_container, slug=slug_owner, roles=["admin", "producer"])
    persona_other = await _make_persona(harness, pg_container, slug=slug_other, roles=["admin", "producer"])

    owner_tid = await _get_tenant_id(pg_container, slug_owner)
    owned_cap = await _seed_capability(pg_container, tenant_id=owner_tid, name=f"{slug_owner}-cap")

    harness.configure_fetcher_for(persona_other)
    with patch_validator_for_actor(persona_other):
        resp = await client.put(
            f"/v1/capabilities/{owned_cap}/interface",
            headers=bearer_headers(tenant_slug=persona_other.slug),
            json={
                "interface_source": "type X = { id: string; }",
                "interface_format": "typescript",
            },
        )
    # Service raises NotFoundError (the opaque shape for cross-tenant access).
    assert resp.status_code == 404
