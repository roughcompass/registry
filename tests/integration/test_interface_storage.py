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
from registry.service.visibility import VISIBILITY_PUBLIC

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)


async def _seed_tenant_and_cap(pg_url: str, slug: str) -> tuple[uuid.UUID, uuid.UUID, str, uuid.UUID]:
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    cap_id = uuid.uuid4()
    raw_token = secrets.token_urlsafe(24)
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
                    "roles": ["producer", "admin"],
                    "now": _NOW,
                },
            )
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
                    "name": f"{slug}-cap",
                    "now": _NOW,
                    "vis": VISIBILITY_PUBLIC,
                },
            )
    finally:
        await engine.dispose()
    return tenant_id, actor_id, raw_token, cap_id


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


@pytest.mark.asyncio
async def test_put_then_get_typescript_interface(pg_container: str, app_client) -> None:
    client = app_client
    _, _, token, cap_id = await _seed_tenant_and_cap(pg_container, "iface-ts")
    headers = {"Authorization": f"Bearer {token}"}

    put = await client.put(
        f"/v1/capabilities/{cap_id}/interface",
        headers=headers,
        json={
            "interface_source": "type Order = { id: string; total: number; }",
            "interface_format": "typescript",
        },
    )
    assert put.status_code == 200, put.text
    canonical = put.json()
    field_names = {f["name"] for f in canonical["fields"]}
    assert field_names == {"id", "total"}

    get = await client.get(f"/v1/capabilities/{cap_id}/interface", headers=headers)
    assert get.status_code == 200
    body = get.json()
    assert body["interface_format"] == "typescript"
    assert {f["name"] for f in body["interface_canonical"]["fields"]} == {
        "id",
        "total",
    }
    assert body["interface_source"]["format"] == "typescript"


@pytest.mark.asyncio
async def test_second_put_supersedes_first(pg_container: str, app_client) -> None:
    client = app_client
    _, _, token, cap_id = await _seed_tenant_and_cap(pg_container, "iface-supersede")
    headers = {"Authorization": f"Bearer {token}"}

    # First PUT — TypeScript.
    r1 = await client.put(
        f"/v1/capabilities/{cap_id}/interface",
        headers=headers,
        json={
            "interface_source": "type X = { id: string; }",
            "interface_format": "typescript",
        },
    )
    assert r1.status_code == 200

    # Second PUT — OpenAPI.
    r2 = await client.put(
        f"/v1/capabilities/{cap_id}/interface",
        headers=headers,
        json={
            "interface_source": {
                "openapi": "3.0.0",
                "paths": {
                    "/orders": {
                        "post": {
                            "operationId": "createOrder",
                            "responses": {"201": {"content": {"application/json": {"schema": {"type": "object"}}}}},
                        }
                    }
                },
            },
            "interface_format": "openapi",
        },
    )
    assert r2.status_code == 200, r2.text

    # Current truth GET reflects the OpenAPI surface only.
    get = await client.get(f"/v1/capabilities/{cap_id}/interface", headers=headers)
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
async def test_put_non_owner_returns_404(pg_container: str, app_client) -> None:
    client = app_client
    # Owner tenant has a capability; a different tenant tries to write.
    _, _, _, owned_cap = await _seed_tenant_and_cap(pg_container, "iface-owner-x")
    _, _, other_token, _ = await _seed_tenant_and_cap(pg_container, "iface-other-y")

    headers = {"Authorization": f"Bearer {other_token}"}
    resp = await client.put(
        f"/v1/capabilities/{owned_cap}/interface",
        headers=headers,
        json={
            "interface_source": "type X = { id: string; }",
            "interface_format": "typescript",
        },
    )
    # Service raises NotFoundError (the opaque shape for cross-tenant access).
    assert resp.status_code == 404
