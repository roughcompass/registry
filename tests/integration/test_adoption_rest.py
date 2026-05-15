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

type _AdoptionHarness = tuple[EntitlementAuthHarness, AsyncClient]

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


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


async def _materialise_persona(
    harness: EntitlementAuthHarness, persona: TenantPersona
) -> uuid.UUID:
    """JIT-materialise tenant + actor and return the tenant_id from the DB."""
    harness.configure_fetcher_for(persona)
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch_validator_for_actor(persona):
            resp = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=persona.slug))
            assert resp.status_code == 200, resp.text
    return uuid.UUID(resp.json()["tenant_id"])


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def adoption_harness(
    pg_container: str,
) -> AsyncIterator[_AdoptionHarness]:
    """Shared harness + client for adoption tests."""
    async with EntitlementAuthHarness(pg_container) as harness:
        transport = ASGITransport(app=harness.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield harness, client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adopt_list_unadopt_full_lifecycle(pg_container: str, adoption_harness: _AdoptionHarness) -> None:
    """The contract's headline scenario: adopt → list → unadopt → no longer
    in list; DB row persists with t_invalidated_at set."""
    harness, client = adoption_harness

    # Provider: tenant A; Consumer: tenant B.
    persona_a = harness.add_persona(f"adopt-rest-a-{uuid.uuid4().hex[:6]}", roles=["producer", "consumer"])
    persona_b = harness.add_persona(f"adopt-rest-b-{uuid.uuid4().hex[:6]}", roles=["producer", "consumer"])

    a_tid = await _materialise_persona(harness, persona_a)
    b_tid = await _materialise_persona(harness, persona_b)

    cap_id = await _seed_capability(
        pg_container,
        tenant_id=a_tid,
        name="payment-api",
        visibility=VISIBILITY_PUBLIC,
    )

    # POST adopt (as tenant B)
    harness.configure_fetcher_for(persona_b)
    with patch_validator_for_actor(persona_b):
        resp = await client.post(
            f"/v1/capabilities/{cap_id}/adoptions",
            headers=bearer_headers(tenant_slug=persona_b.slug),
            json={"intent": "billing reconciliation", "version_pin": ">=1.0,<2.0"},
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    adoption_id = uuid.UUID(body["adoption_id"])
    assert body["provider_capability_id"] == str(cap_id)
    assert body["consumer_tenant_id"] == str(b_tid)
    assert body["tenant_id"] == str(a_tid)  # provider owns the row
    assert body["intent"] == "billing reconciliation"
    assert body["version_pin"] == ">=1.0,<2.0"

    # GET list — caller-scoped, sees its own adoption.
    harness.configure_fetcher_for(persona_b)
    with patch_validator_for_actor(persona_b):
        resp = await client.get(
            f"/v1/capabilities/{cap_id}/adoptions",
            headers=bearer_headers(tenant_slug=persona_b.slug),
        )
    assert resp.status_code == 200
    body = resp.json()
    listed = body["items"] if isinstance(body, dict) else body
    assert len(listed) == 1
    assert listed[0]["adoption_id"] == str(adoption_id)

    # DELETE unadopt
    harness.configure_fetcher_for(persona_b)
    with patch_validator_for_actor(persona_b):
        resp = await client.delete(
            f"/v1/capabilities/{cap_id}/adoptions/{adoption_id}",
            headers=bearer_headers(tenant_slug=persona_b.slug),
        )
    assert resp.status_code == 204, resp.text

    # GET list — empty after unadopt.
    harness.configure_fetcher_for(persona_b)
    with patch_validator_for_actor(persona_b):
        resp = await client.get(
            f"/v1/capabilities/{cap_id}/adoptions",
            headers=bearer_headers(tenant_slug=persona_b.slug),
        )
    assert resp.status_code == 200
    body = resp.json()
    listed_after = body["items"] if isinstance(body, dict) else body
    assert listed_after == []

    # DB row persists with t_invalidated_at set (audit trail).
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await session.execute(
                text("SELECT t_invalidated_at FROM adoption_events WHERE adoption_id = :aid"),
                {"aid": adoption_id},
            )
            row = result.first()
            assert row is not None
            assert row.t_invalidated_at is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_unadopt_is_idempotent(pg_container: str, adoption_harness: _AdoptionHarness) -> None:
    """Two DELETE calls in a row both return 204; the second is a no-op."""
    harness, client = adoption_harness

    persona_a = harness.add_persona(f"adopt-rest-idem-a-{uuid.uuid4().hex[:6]}", roles=["producer"])
    persona_b = harness.add_persona(f"adopt-rest-idem-b-{uuid.uuid4().hex[:6]}", roles=["producer"])

    a_tid = await _materialise_persona(harness, persona_a)
    await _materialise_persona(harness, persona_b)

    cap_id = await _seed_capability(
        pg_container,
        tenant_id=a_tid,
        name="idem-cap",
        visibility=VISIBILITY_PUBLIC,
    )

    harness.configure_fetcher_for(persona_b)
    with patch_validator_for_actor(persona_b):
        resp = await client.post(
            f"/v1/capabilities/{cap_id}/adoptions",
            headers=bearer_headers(tenant_slug=persona_b.slug),
            json={},
        )
    assert resp.status_code == 201
    aid = resp.json()["adoption_id"]

    harness.configure_fetcher_for(persona_b)
    with patch_validator_for_actor(persona_b):
        r1 = await client.delete(
            f"/v1/capabilities/{cap_id}/adoptions/{aid}",
            headers=bearer_headers(tenant_slug=persona_b.slug),
        )
        r2 = await client.delete(
            f"/v1/capabilities/{cap_id}/adoptions/{aid}",
            headers=bearer_headers(tenant_slug=persona_b.slug),
        )
    assert r1.status_code == 204
    assert r2.status_code == 204


@pytest.mark.asyncio
async def test_adopt_private_capability_is_forbidden(pg_container: str, adoption_harness: _AdoptionHarness) -> None:
    """A tenant cannot adopt a private capability owned by another tenant.

    The visibility chokepoint raises PermissionError → 403.
    """
    harness, client = adoption_harness

    persona_a = harness.add_persona(f"adopt-rest-priv-a-{uuid.uuid4().hex[:6]}", roles=["producer"])
    persona_b = harness.add_persona(f"adopt-rest-priv-b-{uuid.uuid4().hex[:6]}", roles=["consumer"])

    a_tid = await _materialise_persona(harness, persona_a)
    await _materialise_persona(harness, persona_b)

    cap_id = await _seed_capability(
        pg_container,
        tenant_id=a_tid,
        name="secret-cap",
        visibility=VISIBILITY_PRIVATE,
    )

    harness.configure_fetcher_for(persona_b)
    with patch_validator_for_actor(persona_b):
        resp = await client.post(
            f"/v1/capabilities/{cap_id}/adoptions",
            headers=bearer_headers(tenant_slug=persona_b.slug),
            json={},
        )
    assert resp.status_code in (403, 404), resp.text
