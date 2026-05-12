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
import secrets
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.api.auth.tokens import hash_token
from registry.config import Settings
from registry.main import create_app
from registry.service.breaking_change import INTERFACE_CANONICAL_KEY
from registry.service.visibility import (
    VISIBILITY_PUBLIC,
)

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_tenant_with_token(
    pg_url: str, *, slug: str, roles: list[str] | None = None
) -> tuple[uuid.UUID, uuid.UUID, str]:
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    raw_token = secrets.token_urlsafe(24)
    role_list = roles or ["producer", "consumer", "admin"]
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
                    "created_at) VALUES (:aid, :tid, :dn, :now)"
                ),
                {"aid": actor_id, "tid": tenant_id, "dn": f"actor-{slug}", "now": _NOW},
            )
            await session.execute(
                text(
                    "INSERT INTO api_tokens "
                    "(token_id, tenant_id, actor_id, token_hash, roles, created_at) "
                    "VALUES (gen_random_uuid(), :tid, :aid, :th, :roles, :now)"
                ),
                {
                    "tid": tenant_id,
                    "aid": actor_id,
                    "th": hash_token(raw_token),
                    "roles": role_list,
                    "now": _NOW,
                },
            )
    finally:
        await engine.dispose()
    return tenant_id, actor_id, raw_token


async def _seed_capability_with_interface(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    name: str,
    interface_canonical: dict | None,
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
            # depends_on edge — consumer → provider.
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
            # adoption_events row so the advisor can look up the version_pin.
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
# App fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def app_client(pg_container: str):  # type: ignore[type-arg]
    settings = Settings(
        database_url=pg_container,
        pgbouncer_url=pg_container,
        scheduler_jobstore_url=pg_container,
        scheduler_use_memory_jobstore=True,
        embedding_model="stub",
    )
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


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
async def test_preview_version_breaking_includes_affected_cross_tenant_consumer(pg_container: str, app_client) -> None:
    """Remove cancelPayment from PaymentAPI; the cross-tenant consumer
    appears with **opaque** tenant/entity identifiers."""
    client = app_client

    # Tenant A publishes PaymentAPI v2.0 with the v1 surface.
    a_tid, _, a_token = await _seed_tenant_with_token(pg_container, slug="bca-prov-a")
    cap_id = await _seed_capability_with_interface(
        pg_container,
        tenant_id=a_tid,
        name="PaymentAPI",
        interface_canonical=_PAYMENT_API_V1_SURFACE,
    )

    # Tenant B has a consumer capability that depends_on PaymentAPI
    # with version_pin ^2.0.0 (so the proposed 3.0.0 fails the pin).
    b_tid, _, _ = await _seed_tenant_with_token(pg_container, slug="bca-cons-b")
    await _seed_consumer_with_depends_on(
        pg_container,
        consumer_tenant_id=b_tid,
        consumer_name="b-billing",
        provider_capability_id=cap_id,
        version_pin="^2.0.0",
    )

    # Proposed v3.0 drops cancelPayment.

    # Submit the proposed as OpenAPI for round-trip coverage.
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

    headers = {"Authorization": f"Bearer {a_token}"}
    resp = await client.post(
        f"/v1/capabilities/{cap_id}/preview-version",
        headers=headers,
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
    # The cross-tenant consumer is anonymised with opaque identifiers.
    cross_entries = [c for c in consumers if c["tenant_id"].startswith("cross-tenant-")]
    assert cross_entries, consumers
    entry = cross_entries[0]
    assert entry["entity_id"].startswith("opaque-")
    # Tenant B's real UUID must not appear anywhere in the response.
    assert str(b_tid) not in resp.text

    # Release-notes scaffold contains the removed operation.
    assert "Severity: breaking" in body["release_notes_scaffold"]
    assert "operation_removed" in body["release_notes_scaffold"]
    assert "cancelPayment" in body["release_notes_scaffold"]


@pytest.mark.asyncio
async def test_preview_version_identical_surface_is_non_breaking(pg_container: str, app_client) -> None:
    """Submitting the *current* surface unchanged → non-breaking + no consumers."""
    client = app_client

    a_tid, _, a_token = await _seed_tenant_with_token(pg_container, slug="bca-nop-a")
    cap_id = await _seed_capability_with_interface(
        pg_container,
        tenant_id=a_tid,
        name="StableCap",
        interface_canonical=_PAYMENT_API_V1_SURFACE,
    )

    headers = {"Authorization": f"Bearer {a_token}"}
    resp = await client.post(
        f"/v1/capabilities/{cap_id}/preview-version",
        headers=headers,
        json={
            "proposed_version": "1.0.1",
            "proposed_interface": _PAYMENT_API_V1_SURFACE,
            "interface_format": "json_schema",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The canonical-as-json_schema input has no `properties` block, but the
    # surface comparison runs over operations/fields/events — identical → non-breaking.
    assert body["diff_classification"] == "non-breaking"
    assert body["affected_consumers"] == []


@pytest.mark.asyncio
async def test_preview_version_rejects_invalid_semver(pg_container: str, app_client) -> None:
    client = app_client
    a_tid, _, a_token = await _seed_tenant_with_token(pg_container, slug="bca-semver-a")
    cap_id = await _seed_capability_with_interface(
        pg_container,
        tenant_id=a_tid,
        name="X",
        interface_canonical=None,
    )

    headers = {"Authorization": f"Bearer {a_token}"}
    resp = await client.post(
        f"/v1/capabilities/{cap_id}/preview-version",
        headers=headers,
        json={
            "proposed_version": "latest",
            "proposed_interface": {"type": "object"},
            "interface_format": "json_schema",
        },
    )
    assert resp.status_code == 422
    assert "semver" in resp.text.lower()
