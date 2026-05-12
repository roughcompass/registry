"""Integration tests for adoption REST endpoints.

Verifies the full HTTP surface against a live FastAPI app + Postgres:

- POST   /v1/capabilities/{cap_id}/adoptions       → 201 + AdoptionResponse
- GET    /v1/capabilities/{cap_id}/adoptions       → list (caller-scoped)
- DELETE /v1/capabilities/{cap_id}/adoptions/{aid} → 204
- Idempotency: DELETE twice → 204 both times; the second is a no-op.
- After unadopt: GET returns []; the DB row persists with t_invalidated_at set.

Visibility precondition: a tenant cannot adopt an invisible capability
(provider's visibility=private). Returns 403.
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
from registry.service.visibility import (
    VISIBILITY_PRIVATE,
    VISIBILITY_PUBLIC,
)

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_tenant_with_token(
    pg_url: str,
    *,
    slug: str,
    roles: list[str] | None = None,
) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Insert (tenant, actor, api_token). Returns (tenant_id, actor_id, raw_token)."""
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


async def _seed_capability(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    name: str,
    visibility: str = VISIBILITY_PUBLIC,
) -> uuid.UUID:
    """Insert one capability entity owned by tenant_id with given visibility."""
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
    finally:
        await engine.dispose()
    return cap_id


# ---------------------------------------------------------------------------
# App fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def app_client(pg_container: str):  # type: ignore[type-arg]
    """A FastAPI app + AsyncClient bound to the live Postgres."""
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
        yield client, settings


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adopt_list_unadopt_full_lifecycle(pg_container: str, app_client) -> None:
    """The contract's headline scenario: adopt → list → unadopt → no longer
    in list; DB row persists with t_invalidated_at set."""
    client, _settings = app_client

    # Provider: tenant A; Consumer: tenant B.
    _a_tid, _a_actor, _a_token = await _seed_tenant_with_token(pg_container, slug="adopt-rest-a")
    b_tid, _b_actor, b_token = await _seed_tenant_with_token(pg_container, slug="adopt-rest-b")
    cap_id = await _seed_capability(
        pg_container,
        tenant_id=_a_tid,
        name="payment-api",
        visibility=VISIBILITY_PUBLIC,
    )

    headers = {"Authorization": f"Bearer {b_token}"}

    # POST adopt
    resp = await client.post(
        f"/v1/capabilities/{cap_id}/adoptions",
        headers=headers,
        json={"intent": "billing reconciliation", "version_pin": ">=1.0,<2.0"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    adoption_id = uuid.UUID(body["adoption_id"])
    assert body["provider_capability_id"] == str(cap_id)
    assert body["consumer_tenant_id"] == str(b_tid)
    assert body["tenant_id"] == str(_a_tid)  # provider owns the row
    assert body["intent"] == "billing reconciliation"
    assert body["version_pin"] == ">=1.0,<2.0"
    assert body["t_invalidated_at"] is None

    # GET list — caller-scoped, sees its own adoption.
    resp = await client.get(f"/v1/capabilities/{cap_id}/adoptions", headers=headers)
    assert resp.status_code == 200
    listed = resp.json()
    assert len(listed) == 1
    assert listed[0]["adoption_id"] == str(adoption_id)

    # DELETE unadopt
    resp = await client.delete(f"/v1/capabilities/{cap_id}/adoptions/{adoption_id}", headers=headers)
    assert resp.status_code == 204, resp.text

    # GET list — empty after unadopt.
    resp = await client.get(f"/v1/capabilities/{cap_id}/adoptions", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == []

    # DB row persists with t_invalidated_at set (audit trail).
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await session.execute(
                text("SELECT t_invalidated_at FROM adoption_events " "WHERE adoption_id = :aid"),
                {"aid": adoption_id},
            )
            row = result.first()
            assert row is not None
            assert row.t_invalidated_at is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_unadopt_is_idempotent(pg_container: str, app_client) -> None:
    """Two DELETE calls in a row both return 204; the second is a no-op."""
    client, _ = app_client

    a_tid, _, _ = await _seed_tenant_with_token(pg_container, slug="adopt-rest-idem-a")
    _b_tid, _, b_token = await _seed_tenant_with_token(pg_container, slug="adopt-rest-idem-b")
    cap_id = await _seed_capability(
        pg_container,
        tenant_id=a_tid,
        name="idem-cap",
        visibility=VISIBILITY_PUBLIC,
    )

    headers = {"Authorization": f"Bearer {b_token}"}
    resp = await client.post(f"/v1/capabilities/{cap_id}/adoptions", headers=headers, json={})
    assert resp.status_code == 201
    aid = resp.json()["adoption_id"]

    r1 = await client.delete(f"/v1/capabilities/{cap_id}/adoptions/{aid}", headers=headers)
    r2 = await client.delete(f"/v1/capabilities/{cap_id}/adoptions/{aid}", headers=headers)
    assert r1.status_code == 204
    assert r2.status_code == 204


@pytest.mark.asyncio
async def test_adopt_private_capability_is_forbidden(pg_container: str, app_client) -> None:
    """A tenant cannot adopt a private capability owned by another tenant.

    The visibility chokepoint raises PermissionError → 403.
    """
    client, _ = app_client

    a_tid, _, _ = await _seed_tenant_with_token(pg_container, slug="adopt-rest-priv-a")
    _, _, b_token = await _seed_tenant_with_token(pg_container, slug="adopt-rest-priv-b")
    cap_id = await _seed_capability(
        pg_container,
        tenant_id=a_tid,
        name="secret-cap",
        visibility=VISIBILITY_PRIVATE,
    )

    headers = {"Authorization": f"Bearer {b_token}"}
    resp = await client.post(f"/v1/capabilities/{cap_id}/adoptions", headers=headers, json={})
    assert resp.status_code in (403, 404), resp.text
