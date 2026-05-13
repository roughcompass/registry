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


async def _seed_tenant(pg_url: str, slug: str) -> tuple[uuid.UUID, uuid.UUID, str]:
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
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
                    "roles": ["producer", "consumer", "admin"],
                    "now": _NOW,
                },
            )
    finally:
        await engine.dispose()
    return tenant_id, actor_id, raw_token


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
# Scenario 1 — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pair_lookup_returns_integration_connecting_two_caps(pg_container: str, app_client) -> None:
    client = app_client
    tid, _, token = await _seed_tenant(pg_container, "intpairs-h")

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
                text("SELECT COUNT(*) FROM integration_pairs " "WHERE integration_entity_id = :i"),
                {"i": integration},
            )
            assert (res.scalar() or 0) == 2
    finally:
        await engine.dispose()

    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.get(
        "/v1/integrations",
        headers=headers,
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
async def test_private_integration_is_invisible_to_other_tenants(pg_container: str, app_client) -> None:
    client = app_client
    a_tid, _, _ = await _seed_tenant(pg_container, "intpairs-priv-a")
    b_tid, _, b_token = await _seed_tenant(pg_container, "intpairs-priv-b")

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

    headers = {"Authorization": f"Bearer {b_token}"}
    resp = await client.get(
        "/v1/integrations",
        headers=headers,
        params={"connects": str(cap_a), "and": str(cap_b)},
    )
    assert resp.status_code == 200
    body = resp.json()
    items = body["items"] if isinstance(body, dict) else body
    assert items == []
