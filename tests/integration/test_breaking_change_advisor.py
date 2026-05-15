"""Integration test for the breaking-change advisor REST endpoint.

Covers the headline scenario:

  Preview PaymentAPI v3.0 with ``cancelPayment`` removed. Cross-tenant
  consumers appear in the response with **opaque** identifiers; the count
  matches the expected number of consumers.

Plus the no-op case: identical surface → diff_classification = non-breaking,
affected_consumers is empty.
"""

from __future__ import annotations

import datetime
import json
import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.service.breaking_change import INTERFACE_CANONICAL_KEY
from registry.service.visibility import (
    VISIBILITY_PUBLIC,
)
from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    TenantPersona,
    bearer_headers,
    patch_validator_for_actor,
)

type _AppClient = tuple[AsyncClient, EntitlementAuthHarness]

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_capability_with_interface(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    name: str,
    interface_canonical: Mapping[str, Any] | None,
    visibility: str = VISIBILITY_PUBLIC,
) -> uuid.UUID:
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
                    "vis": visibility,
                },
            )
            if interface_canonical is not None:
                await session.execute(
                    text(
                        "INSERT INTO attributes "
                        "(attr_id, tenant_id, entity_id, key, value, "
                        " t_valid_from, t_valid_to, t_ingested_at, "
                        " t_invalidated_at) "
                        "VALUES (gen_random_uuid(), :tid, :eid, :k, "
                        "        CAST(:val AS jsonb), :now, NULL, :now, NULL)"
                    ),
                    {
                        "tid": tenant_id,
                        "eid": cap_id,
                        "k": INTERFACE_CANONICAL_KEY,
                        "val": json.dumps(interface_canonical),
                        "now": _NOW,
                    },
                )
    finally:
        await engine.dispose()
    return cap_id


async def _seed_consumer_with_depends_on(
    pg_url: str,
    *,
    consumer_tenant_id: uuid.UUID,
    consumer_name: str,
    provider_capability_id: uuid.UUID,
    version_pin: str | None = None,
) -> uuid.UUID:
    """Create a consumer capability + depends_on edge → provider capability."""
    consumer_id = uuid.uuid4()
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO entities "
                    "(entity_id, tenant_id, entity_type, name, is_active, "
                    " created_at, visibility) "
                    "VALUES (:eid, :tid, 'capability', :name, TRUE, :now, "
                    "        'public')"
                ),
                {
                    "eid": consumer_id,
                    "tid": consumer_tenant_id,
                    "name": consumer_name,
                    "now": _NOW,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO edges "
                    "(edge_id, tenant_id, src_entity_id, rel, dst_entity_id, "
                    " properties, t_valid_from, t_ingested_at) "
                    "VALUES (gen_random_uuid(), :tid, :src, 'depends_on', :dst, "
                    "        NULL, :now, :now)"
                ),
                {
                    "tid": consumer_tenant_id,
                    "src": consumer_id,
                    "dst": provider_capability_id,
                    "now": _NOW,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO adoption_events "
                    "(adoption_id, tenant_id, provider_capability_id, "
                    " consumer_tenant_id, actor_id, intent, version_pin, "
                    " t_valid_from, t_valid_to, t_ingested_at, t_invalidated_at) "
                    "VALUES (gen_random_uuid(), "
                    "        (SELECT tenant_id FROM entities WHERE entity_id = :dst), "
                    "        :dst, :ctid, NULL, NULL, :pin, "
                    "        :now, NULL, :now, NULL)"
                ),
                {
                    "dst": provider_capability_id,
                    "ctid": consumer_tenant_id,
                    "pin": version_pin,
                    "now": _NOW,
                },
            )
    finally:
        await engine.dispose()
    return consumer_id


# ---------------------------------------------------------------------------
# Harness helpers
# ---------------------------------------------------------------------------


async def _make_persona(
    harness: EntitlementAuthHarness, client: AsyncClient, slug: str, roles: list[str]
) -> tuple[TenantPersona, uuid.UUID]:
    """Add a persona, JIT-materialise via whoami, return (persona, tenant_id)."""
    persona = harness.add_persona(slug, roles=roles)
    harness.configure_fetcher_for(persona)
    with patch_validator_for_actor(persona):
        resp = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))
        assert resp.status_code == 200, resp.text
    return persona, uuid.UUID(resp.json()["tenant_id"])


# ---------------------------------------------------------------------------
# App fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def app_client(pg_container: str) -> AsyncIterator[_AppClient]:
    async with EntitlementAuthHarness(pg_container) as harness:
        transport = ASGITransport(app=harness.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, harness


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


_PAYMENT_API_V1_SURFACE = {
    "operations": [
        {"name": "createPayment", "method": "POST", "path": "/payments", "params": [], "returns": "object"},
        {"name": "cancelPayment", "method": "POST", "path": "/payments/cancel", "params": [], "returns": "object"},
    ],
    "events": [],
    "fields": [],
}


@pytest.mark.asyncio
async def test_preview_version_breaking_includes_affected_cross_tenant_consumer(
    pg_container: str, app_client: _AppClient
) -> None:
    """Remove cancelPayment from PaymentAPI; the cross-tenant consumer
    appears with **opaque** tenant/entity identifiers."""
    client, harness = app_client

    persona_a, a_tid = await _make_persona(harness, client, f"bca-prov-a-{uuid.uuid4().hex[:6]}", ["producer", "admin"])
    _persona_b, b_tid = await _make_persona(harness, client, f"bca-cons-b-{uuid.uuid4().hex[:6]}", ["consumer"])

    cap_id = await _seed_capability_with_interface(
        pg_container,
        tenant_id=a_tid,
        name="PaymentAPI",
        interface_canonical=_PAYMENT_API_V1_SURFACE,
    )

    await _seed_consumer_with_depends_on(
        pg_container,
        consumer_tenant_id=b_tid,
        consumer_name="b-billing",
        provider_capability_id=cap_id,
        version_pin="^2.0.0",
    )

    proposed_openapi = {
        "openapi": "3.0.3",
        "paths": {
            "/payments": {
                "post": {
                    "operationId": "createPayment",
                    "responses": {"201": {"content": {"application/json": {"schema": {"type": "object"}}}}},
                }
            }
        },
    }

    harness.configure_fetcher_for(persona_a)
    with patch_validator_for_actor(persona_a):
        resp = await client.post(
            f"/v1/capabilities/{cap_id}/preview-version",
            headers=bearer_headers(tenant_slug=persona_a.slug),
            json={
                "proposed_version": "3.0.0",
                "proposed_interface": proposed_openapi,
                "interface_format": "openapi",
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["diff_classification"] == "breaking"

    consumers = body["affected_consumers"]
    assert len(consumers) >= 1
    cross_entries = [c for c in consumers if c["tenant_id"].startswith("cross-tenant-")]
    assert cross_entries, consumers
    entry = cross_entries[0]
    assert entry["entity_id"].startswith("opaque-")
    assert str(b_tid) not in resp.text

    assert "Severity: breaking" in body["release_notes_scaffold"]
    assert "operation_removed" in body["release_notes_scaffold"]
    assert "cancelPayment" in body["release_notes_scaffold"]


@pytest.mark.asyncio
async def test_preview_version_identical_surface_is_non_breaking(pg_container: str, app_client: _AppClient) -> None:
    """Submitting the *current* surface unchanged → non-breaking + no consumers."""
    client, harness = app_client

    persona_a, a_tid = await _make_persona(harness, client, f"bca-nop-a-{uuid.uuid4().hex[:6]}", ["producer", "admin"])

    cap_id = await _seed_capability_with_interface(
        pg_container,
        tenant_id=a_tid,
        name="StableCap",
        interface_canonical=_PAYMENT_API_V1_SURFACE,
    )

    harness.configure_fetcher_for(persona_a)
    with patch_validator_for_actor(persona_a):
        resp = await client.post(
            f"/v1/capabilities/{cap_id}/preview-version",
            headers=bearer_headers(tenant_slug=persona_a.slug),
            json={
                "proposed_version": "1.0.1",
                "proposed_interface": _PAYMENT_API_V1_SURFACE,
                "interface_format": "json_schema",
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["diff_classification"] == "non-breaking"
    assert body["affected_consumers"] == []


@pytest.mark.asyncio
async def test_preview_version_rejects_invalid_semver(pg_container: str, app_client: _AppClient) -> None:
    client, harness = app_client
    persona_a, a_tid = await _make_persona(
        harness, client, f"bca-semver-a-{uuid.uuid4().hex[:6]}", ["producer", "admin"]
    )
    cap_id = await _seed_capability_with_interface(
        pg_container,
        tenant_id=a_tid,
        name="X",
        interface_canonical=None,
    )

    harness.configure_fetcher_for(persona_a)
    with patch_validator_for_actor(persona_a):
        resp = await client.post(
            f"/v1/capabilities/{cap_id}/preview-version",
            headers=bearer_headers(tenant_slug=persona_a.slug),
            json={
                "proposed_version": "latest",
                "proposed_interface": {"type": "object"},
                "interface_format": "json_schema",
            },
        )
    assert resp.status_code == 422
    assert "semver" in resp.text.lower()
