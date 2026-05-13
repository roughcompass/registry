"""Role enforcement regression gate for entity-create endpoints.

Asserts that POST create handlers for capabilities, concepts, operations,
and artifacts require at least producer or admin role. A consumer-role-only
token must receive 403; a producer-role token must succeed (201).

Each mutating surface touched by CPR-T01 gets one 403 probe and one 201
happy-path probe so a future regression restoring the bare get_tenant_context
dependency is caught immediately.
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

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Seed helpers (inline — avoids coupling to other integration fixtures)
# ---------------------------------------------------------------------------


async def _seed_tenant(
    pg_url: str,
    *,
    slug: str,
    roles: list[str],
) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Create a tenant + actor + api_token with the given roles. Returns (tenant_id, actor_id, raw_token)."""
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
                    "VALUES (gen_random_uuid(), :tid, :aid, :th, :roles, :now)"
                ),
                {
                    "tid": tenant_id,
                    "aid": actor_id,
                    "th": hash_token(raw_token),
                    "roles": roles,
                    "now": _NOW,
                },
            )
    finally:
        await engine.dispose()
    return tenant_id, actor_id, raw_token


async def _seed_capability_row(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    name: str,
) -> uuid.UUID:
    """Insert a minimal capability row directly (bypasses role check — for test setup only)."""
    cap_id = uuid.uuid4()
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO entities "
                    "(entity_id, tenant_id, entity_type, name, is_active, created_at, visibility) "
                    "VALUES (:eid, :tid, 'capability', :name, TRUE, :now, 'private')"
                ),
                {"eid": cap_id, "tid": tenant_id, "name": name, "now": _NOW},
            )
    finally:
        await engine.dispose()
    return cap_id


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def enforcement_clients(pg_container: str):
    """Yield (consumer_client, producer_client, cap_id) for the role-enforcement tests.

    Both clients target the same tenant. The consumer token carries only
    ['consumer']; the producer token carries ['producer']. A capability row
    is pre-seeded so PATCH / DELETE / artifact create tests have something to
    operate on.
    """
    slug_c = f"re-consumer-{secrets.token_hex(4)}"
    slug_p = f"re-producer-{secrets.token_hex(4)}"

    tenant_id, _, consumer_token = await _seed_tenant(pg_container, slug=slug_c, roles=["consumer"])
    # Producer tenant is separate — this lets us also confirm the cross-tenant
    # isolation still holds (producer can't mutate another tenant's rows), but
    # for same-tenant tests we seed both tokens under the same tenant_id.
    _, _, producer_token = await _seed_tenant(pg_container, slug=slug_p, roles=["producer"])
    # Re-seed producer token under the consumer's tenant so happy-path create
    # lands in the same tenant scope.
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    producer_actor_id = uuid.uuid4()
    prod_raw = secrets.token_urlsafe(24)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, display_name, created_at) "
                    "VALUES (:aid, :tid, 'producer-actor', :now)"
                ),
                {"aid": producer_actor_id, "tid": tenant_id, "now": _NOW},
            )
            await session.execute(
                text(
                    "INSERT INTO api_tokens "
                    "(token_id, tenant_id, actor_id, token_hash, roles, created_at) "
                    "VALUES (gen_random_uuid(), :tid, :aid, :th, :roles, :now)"
                ),
                {
                    "tid": tenant_id,
                    "aid": producer_actor_id,
                    "th": hash_token(prod_raw),
                    "roles": ["producer"],
                    "now": _NOW,
                },
            )
    finally:
        await engine.dispose()

    cap_id = await _seed_capability_row(pg_container, tenant_id=tenant_id, name="role-enforcement-cap")

    settings = Settings(
        database_url=pg_container,
        pgbouncer_url=pg_container,
        scheduler_jobstore_url=pg_container,
        scheduler_use_memory_jobstore=True,
        embedding_model="stub",
    )
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as consumer_client:
        async with AsyncClient(transport=transport, base_url="http://test") as producer_client:
            consumer_client.headers.update({"Authorization": f"Bearer {consumer_token}"})
            producer_client.headers.update({"Authorization": f"Bearer {prod_raw}"})
            yield consumer_client, producer_client, cap_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assert_forbidden(response) -> None:
    """Assert the response is 403 with the expected envelope code."""
    assert response.status_code == 403, f"expected 403, got {response.status_code}: {response.text}"
    body = response.json()
    assert "errors" in body, f"no 'errors' key in response: {body}"
    codes = [e.get("code") for e in body["errors"]]
    assert "forbidden" in codes, f"expected code 'forbidden' in errors, got: {codes}"


# ---------------------------------------------------------------------------
# POST /v1/capabilities — consumer must get 403, producer must get 201
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_capability_consumer_forbidden(
    enforcement_clients,
) -> None:
    consumer_client, _, _ = enforcement_clients
    resp = await consumer_client.post(
        "/v1/capabilities",
        json={"name": "should-be-denied", "capability_type": "component"},
    )
    _assert_forbidden(resp)


@pytest.mark.asyncio
async def test_create_capability_producer_succeeds(
    enforcement_clients,
) -> None:
    _, producer_client, _ = enforcement_clients
    resp = await producer_client.post(
        "/v1/capabilities",
        json={"name": f"producer-cap-{secrets.token_hex(4)}", "capability_type": "component"},
    )
    assert resp.status_code == 201, f"expected 201, got {resp.status_code}: {resp.text}"


# ---------------------------------------------------------------------------
# POST /v1/concepts — consumer must get 403, producer must get 201
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_concept_consumer_forbidden(
    enforcement_clients,
) -> None:
    consumer_client, _, cap_id = enforcement_clients
    resp = await consumer_client.post(
        "/v1/concepts",
        json={
            "name": "denied-concept",
            "entity_type": "concept",
            "parent_capability_id": str(cap_id),
        },
    )
    _assert_forbidden(resp)


@pytest.mark.asyncio
async def test_create_concept_producer_succeeds(
    enforcement_clients,
) -> None:
    _, producer_client, cap_id = enforcement_clients
    resp = await producer_client.post(
        "/v1/concepts",
        json={
            "name": f"ok-concept-{secrets.token_hex(4)}",
            "entity_type": "concept",
            "parent_capability_id": str(cap_id),
        },
    )
    assert resp.status_code == 201, f"expected 201, got {resp.status_code}: {resp.text}"


# ---------------------------------------------------------------------------
# POST /v1/operations — consumer must get 403, producer must get 201
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_operation_consumer_forbidden(
    enforcement_clients,
) -> None:
    consumer_client, _, cap_id = enforcement_clients
    resp = await consumer_client.post(
        "/v1/operations",
        json={
            "name": "denied-op",
            "entity_type": "operation",
            "parent_capability_id": str(cap_id),
        },
    )
    _assert_forbidden(resp)


@pytest.mark.asyncio
async def test_create_operation_producer_succeeds(
    enforcement_clients,
) -> None:
    _, producer_client, cap_id = enforcement_clients
    resp = await producer_client.post(
        "/v1/operations",
        json={
            "name": f"ok-op-{secrets.token_hex(4)}",
            "entity_type": "operation",
            "parent_capability_id": str(cap_id),
        },
    )
    assert resp.status_code == 201, f"expected 201, got {resp.status_code}: {resp.text}"


# ---------------------------------------------------------------------------
# POST /v1/capabilities/{id}/artifacts — consumer must get 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_artifact_consumer_forbidden(
    enforcement_clients,
) -> None:
    consumer_client, _, cap_id = enforcement_clients
    resp = await consumer_client.post(
        f"/v1/capabilities/{cap_id}/artifacts",
        json={"category": "overview", "body": "some text"},
    )
    _assert_forbidden(resp)


@pytest.mark.asyncio
async def test_create_artifact_producer_succeeds(
    enforcement_clients,
) -> None:
    _, producer_client, cap_id = enforcement_clients
    resp = await producer_client.post(
        f"/v1/capabilities/{cap_id}/artifacts",
        json={
            "category": "overview",
            "title": "Producer Test Artifact",
            "body": "artifact body from producer",
        },
    )
    assert resp.status_code == 201, f"expected 201, got {resp.status_code}: {resp.text}"
