"""Role enforcement regression gate for entity-create endpoints.

Asserts that POST create handlers for capabilities, concepts, operations,
and artifacts require at least producer or admin role. A consumer-role-only
actor must receive 403; a producer-role actor must succeed (201).

Each mutating surface gets one 403 probe and one 201 happy-path probe so a
future regression restoring the bare get_tenant_context dependency is caught
immediately.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    TenantPersona,
    bearer_headers,
    patch_validator_for_actor,
)

_VOCAB_ROWS = [
    ("entity_type", "capability"),
    ("entity_type", "concept"),
    ("entity_type", "operation"),
    ("fact_category", "overview"),
    ("fact_category", "adr"),
    ("edge_rel", "concept_of"),
    ("edge_rel", "operation_of"),
    ("edge_rel", "depends_on"),
]


async def _seed_vocabulary(pg_url: str, tenant_id: uuid.UUID) -> None:
    """Seed minimum vocabulary so entity-create endpoints don't reject the type."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            for kind, value in _VOCAB_ROWS:
                await session.execute(
                    text(
                        "INSERT INTO vocabulary_values (tenant_id, kind, value, is_system) "
                        "VALUES (:tid, :kind, :value, FALSE) ON CONFLICT DO NOTHING"
                    ),
                    {"tid": tenant_id, "kind": kind, "value": value},
                )
    finally:
        await engine.dispose()


async def _seed_capability_row(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
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
                    "(entity_id, tenant_id, entity_type, name, is_active, created_at, created_by) "
                    "VALUES (:eid, :tid, 'capability', :name, TRUE, now(), :aid)"
                ),
                {"eid": cap_id, "tid": tenant_id, "name": name, "aid": actor_id},
            )
    finally:
        await engine.dispose()
    return cap_id


async def _jit_materialise(
    harness: EntitlementAuthHarness,
    persona: TenantPersona,
) -> uuid.UUID:
    """Drive JIT tenant materialisation; return the DB tenant_id."""
    harness.configure_fetcher_for(persona)
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch_validator_for_actor(persona):
            resp = await client.get(
                "/v1/whoami", headers=bearer_headers(tenant_slug=persona.slug)
            )
            assert resp.status_code == 200, resp.text
            return uuid.UUID(resp.json()["tenant_id"])


class _EnforcementFixture:
    """Bundles the harness, personas, client, and a pre-seeded capability id."""

    def __init__(
        self,
        harness: EntitlementAuthHarness,
        consumer: TenantPersona,
        producer: TenantPersona,
        cap_id: uuid.UUID,
        pg_url: str,
    ) -> None:
        self.harness = harness
        self.consumer = consumer
        self.producer = producer
        self.cap_id = cap_id
        self.pg_url = pg_url


@pytest_asyncio.fixture
async def enforcement_clients(pg_container: str) -> AsyncIterator[_EnforcementFixture]:
    """Yield an _EnforcementFixture for the role-enforcement tests.

    Both personas share the same tenant (same slug). The consumer persona
    carries only ['consumer']; the producer persona carries only ['producer'].
    A capability row is pre-seeded so PATCH / artifact create tests have a
    target entity.
    """
    suffix = secrets.token_hex(4)
    slug = f"re-shared-{suffix}"

    async with EntitlementAuthHarness(pg_container) as harness:
        consumer_persona = harness.add_persona(slug, roles=["consumer"])
        producer_persona = harness.add_persona(slug, roles=["producer"])

        # Materialise tenant via the producer persona (producer role satisfies
        # the harness JIT path); consumer persona shares the same slug/tenant.
        tenant_id = await _jit_materialise(harness, producer_persona)
        await _seed_vocabulary(pg_container, tenant_id)

        # Look up the harness's actor_id for the producer (for capability seed).
        engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                row = (
                    await session.execute(
                        text("SELECT actor_id FROM actors WHERE tenant_id = :tid LIMIT 1"),
                        {"tid": tenant_id},
                    )
                ).first()
        finally:
            await engine.dispose()
        assert row is not None
        actor_id = row[0]

        cap_id = await _seed_capability_row(
            pg_container,
            tenant_id=tenant_id,
            actor_id=actor_id,
            name="role-enforcement-cap",
        )

        yield _EnforcementFixture(harness, consumer_persona, producer_persona, cap_id, pg_container)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assert_forbidden(response) -> None:  # type: ignore[type-arg]
    """Assert the response is 403 with the expected envelope code."""
    assert response.status_code == 403, f"expected 403, got {response.status_code}: {response.text}"
    body = response.json()
    assert "errors" in body or "detail" in body, f"no error key in response: {body}"
    if "errors" in body:
        codes = [e.get("code") for e in body["errors"]]
        assert "forbidden" in codes, f"expected code 'forbidden' in errors, got: {codes}"


# ---------------------------------------------------------------------------
# POST /v1/capabilities -- consumer must get 403, producer must get 201
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_capability_consumer_forbidden(
    enforcement_clients: _EnforcementFixture,
) -> None:
    harness = enforcement_clients.harness
    persona = enforcement_clients.consumer
    harness.configure_fetcher_for(persona)
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch_validator_for_actor(persona):
            resp = await client.post(
                "/v1/capabilities",
                json={"name": "should-be-denied", "capability_type": "component"},
                headers=bearer_headers(tenant_slug=persona.slug),
            )
    _assert_forbidden(resp)


@pytest.mark.asyncio
async def test_create_capability_producer_succeeds(
    enforcement_clients: _EnforcementFixture,
) -> None:
    harness = enforcement_clients.harness
    persona = enforcement_clients.producer
    harness.configure_fetcher_for(persona)
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch_validator_for_actor(persona):
            resp = await client.post(
                "/v1/capabilities",
                json={"name": f"producer-cap-{secrets.token_hex(4)}", "capability_type": "component"},
                headers=bearer_headers(tenant_slug=persona.slug),
            )
    assert resp.status_code == 201, f"expected 201, got {resp.status_code}: {resp.text}"


# ---------------------------------------------------------------------------
# POST /v1/concepts -- consumer must get 403, producer must get 201
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_concept_consumer_forbidden(
    enforcement_clients: _EnforcementFixture,
) -> None:
    harness = enforcement_clients.harness
    persona = enforcement_clients.consumer
    cap_id = enforcement_clients.cap_id
    harness.configure_fetcher_for(persona)
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch_validator_for_actor(persona):
            resp = await client.post(
                "/v1/concepts",
                json={
                    "name": "denied-concept",
                    "entity_type": "concept",
                    "parent_capability_id": str(cap_id),
                },
                headers=bearer_headers(tenant_slug=persona.slug),
            )
    _assert_forbidden(resp)


@pytest.mark.asyncio
async def test_create_concept_producer_succeeds(
    enforcement_clients: _EnforcementFixture,
) -> None:
    harness = enforcement_clients.harness
    persona = enforcement_clients.producer
    cap_id = enforcement_clients.cap_id
    harness.configure_fetcher_for(persona)
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch_validator_for_actor(persona):
            resp = await client.post(
                "/v1/concepts",
                json={
                    "name": f"ok-concept-{secrets.token_hex(4)}",
                    "entity_type": "concept",
                    "parent_capability_id": str(cap_id),
                },
                headers=bearer_headers(tenant_slug=persona.slug),
            )
    assert resp.status_code == 201, f"expected 201, got {resp.status_code}: {resp.text}"


# ---------------------------------------------------------------------------
# POST /v1/operations -- consumer must get 403, producer must get 201
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_operation_consumer_forbidden(
    enforcement_clients: _EnforcementFixture,
) -> None:
    harness = enforcement_clients.harness
    persona = enforcement_clients.consumer
    cap_id = enforcement_clients.cap_id
    harness.configure_fetcher_for(persona)
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch_validator_for_actor(persona):
            resp = await client.post(
                "/v1/operations",
                json={
                    "name": "denied-op",
                    "entity_type": "operation",
                    "parent_capability_id": str(cap_id),
                },
                headers=bearer_headers(tenant_slug=persona.slug),
            )
    _assert_forbidden(resp)


@pytest.mark.asyncio
async def test_create_operation_producer_succeeds(
    enforcement_clients: _EnforcementFixture,
) -> None:
    harness = enforcement_clients.harness
    persona = enforcement_clients.producer
    cap_id = enforcement_clients.cap_id
    harness.configure_fetcher_for(persona)
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch_validator_for_actor(persona):
            resp = await client.post(
                "/v1/operations",
                json={
                    "name": f"ok-op-{secrets.token_hex(4)}",
                    "entity_type": "operation",
                    "parent_capability_id": str(cap_id),
                },
                headers=bearer_headers(tenant_slug=persona.slug),
            )
    assert resp.status_code == 201, f"expected 201, got {resp.status_code}: {resp.text}"


# ---------------------------------------------------------------------------
# POST /v1/capabilities/{id}/artifacts -- consumer must get 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_artifact_consumer_forbidden(
    enforcement_clients: _EnforcementFixture,
) -> None:
    harness = enforcement_clients.harness
    persona = enforcement_clients.consumer
    cap_id = enforcement_clients.cap_id
    harness.configure_fetcher_for(persona)
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch_validator_for_actor(persona):
            resp = await client.post(
                f"/v1/capabilities/{cap_id}/artifacts",
                json={"category": "overview", "body": "some text"},
                headers=bearer_headers(tenant_slug=persona.slug),
            )
    _assert_forbidden(resp)


@pytest.mark.asyncio
async def test_create_artifact_producer_succeeds(
    enforcement_clients: _EnforcementFixture,
) -> None:
    harness = enforcement_clients.harness
    persona = enforcement_clients.producer
    cap_id = enforcement_clients.cap_id
    harness.configure_fetcher_for(persona)
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch_validator_for_actor(persona):
            resp = await client.post(
                f"/v1/capabilities/{cap_id}/artifacts",
                json={
                    "category": "overview",
                    "title": "Producer Test Artifact",
                    "body": "artifact body from producer",
                },
                headers=bearer_headers(tenant_slug=persona.slug),
            )
    assert resp.status_code == 201, f"expected 201, got {resp.status_code}: {resp.text}"
