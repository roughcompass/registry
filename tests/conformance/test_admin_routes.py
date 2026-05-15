"""Admin progression routes — RBAC conformance.

Enumerates every progression admin endpoint and asserts that each
returns 403 when called by an actor whose entitlement-resolved roles
do not include ``admin``. The check is explicit (one test per
endpoint) rather than auto-enumerated from the OpenAPI spec so it
stays stable across spec regeneration cycles and does not require a
live spec export.

Covered endpoints
-----------------
  POST   /v1/admin/tenants/{tenant_id}/progression-definitions
  GET    /v1/admin/tenants/{tenant_id}/progression-definitions
  GET    /v1/admin/tenants/{tenant_id}/progression-definitions/{id}
  PUT    /v1/admin/tenants/{tenant_id}/progression-definitions/{id}
  DELETE /v1/admin/tenants/{tenant_id}/progression-definitions/{id}
  POST   /v1/admin/tenants/{tenant_id}/entities/{entity_id}/progression-overrides
  GET    /v1/admin/tenants/{tenant_id}/entities/{entity_id}/progression-overrides

Auth setup mirrors the rest of the conformance suite: the OIDC
validator is patched to return a fixed identity, and the entitlement
resolver's fetcher is mocked to return whatever roles the test wants
the actor to hold.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import NamedTuple

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

# A definition body the API accepts. Kept minimal — the tests don't care
# about the definition's contents, only that the endpoint reaches the
# auth gate and that the gate denies non-admin callers.
_DEFINITION_BODY: dict[str, object] = {
    "states": [
        {"id": "draft", "name": "Draft"},
        {"id": "published", "name": "Published"},
    ],
    "transitions": {"forward": "sequential"},
}


class _AdminRbacHarness(NamedTuple):
    tenant_slug: str
    tenant_id: uuid.UUID
    entity_id: uuid.UUID
    progression_id: str
    norole_persona: TenantPersona
    admin_persona: TenantPersona
    harness: EntitlementAuthHarness
    client: AsyncClient


async def _materialise_persona(
    harness: EntitlementAuthHarness, client: AsyncClient, persona: TenantPersona
) -> None:
    """Drive a single /v1/whoami call so the resolver materialises the
    tenant + actor row JIT before any admin endpoint is hit."""
    harness.configure_fetcher_for(persona)
    with patch_validator_for_actor(persona):
        resp = await client.get(
            "/v1/whoami", headers=bearer_headers(tenant_slug=persona.slug)
        )
        assert resp.status_code == 200, resp.text


async def _seed_entity_in_tenant(
    pg_url: str, tenant_id: uuid.UUID
) -> uuid.UUID:
    """Insert a minimal entity row owned by ``tenant_id`` so the
    progression-overrides endpoints have a real entity_id to address."""
    entity_id = uuid.uuid4()
    engine = create_async_engine(
        pg_url, connect_args={"prepared_statement_cache_size": 0}
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO entities "
                    "(entity_id, tenant_id, entity_type, name, is_active, created_at) "
                    "VALUES (:eid, :tid, 'capability', :name, TRUE, now())"
                ),
                {"eid": entity_id, "tid": tenant_id, "name": f"ent-{entity_id}"},
            )
    finally:
        await engine.dispose()
    return entity_id


async def _lookup_tenant_id(pg_url: str, slug: str) -> uuid.UUID:
    engine = create_async_engine(
        pg_url, connect_args={"prepared_statement_cache_size": 0}
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            row = (
                await session.execute(
                    text("SELECT tenant_id FROM tenants WHERE slug = :slug"),
                    {"slug": slug},
                )
            ).first()
        assert row is not None
        return uuid.UUID(str(row[0]))
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def admin_rbac_harness(pg_container: str) -> AsyncIterator[_AdminRbacHarness]:
    """Build a harness with two personas in the same tenant: one with admin
    grant, one with no roles. Seed a progression definition so PUT/GET-by-id/
    DELETE paths have a real progression_id to address."""
    slug = f"rbac-prog-{uuid.uuid4().hex[:8]}"
    async with EntitlementAuthHarness(pg_container) as harness:
        admin_persona = harness.add_persona(slug, roles=["admin"])
        # Build the no-role persona inside the same tenant slug. The
        # entitlement resolver returns an empty grant set when fed an
        # empty role list — that's the "no role in this tenant"
        # scenario the test needs.
        norole_persona = TenantPersona(
            slug=slug, actor_id=uuid.uuid4(), roles=[]
        )

        transport = ASGITransport(app=harness.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # JIT-materialise the tenant via the admin persona (a no-role
            # persona's resolved grant set is empty, so it would 403 on
            # /v1/whoami before the tenant row gets created).
            await _materialise_persona(harness, client, admin_persona)
            tenant_id = await _lookup_tenant_id(pg_container, slug)
            entity_id = await _seed_entity_in_tenant(pg_container, tenant_id)

            # Create a definition as admin so the by-id paths have an id.
            harness.configure_fetcher_for(admin_persona)
            with patch_validator_for_actor(admin_persona):
                def_resp = await client.post(
                    f"/v1/admin/tenants/{tenant_id}/progression-definitions",
                    json={
                        "entity_type": f"et-rbac-{uuid.uuid4().hex[:6]}",
                        "definition": _DEFINITION_BODY,
                        "is_advisory": True,
                    },
                    headers=bearer_headers(tenant_slug=slug),
                )
            assert def_resp.status_code == 201, def_resp.text
            progression_id = def_resp.json()["progression_id"]

            yield _AdminRbacHarness(
                tenant_slug=slug,
                tenant_id=tenant_id,
                entity_id=entity_id,
                progression_id=progression_id,
                norole_persona=norole_persona,
                admin_persona=admin_persona,
                harness=harness,
                client=client,
            )


# ---------------------------------------------------------------------------
# 403-or-401 for no-role caller — every progression admin endpoint
# ---------------------------------------------------------------------------
#
# The exact denial code depends on whether the resolver's empty-grant
# fast-path fires before the role check (401 "access denied" surfaces
# from EntitlementNotFoundError-style paths) or the role check itself
# rejects (403). Both are correct denial outcomes; the conformance
# guarantee is "the call MUST NOT succeed" — i.e. status >= 400 and
# != 200/201/204.
# ---------------------------------------------------------------------------


def _denied(status_code: int) -> bool:
    return status_code in (401, 403)


@pytest.mark.asyncio
async def test_post_progression_definition_requires_admin(
    admin_rbac_harness: _AdminRbacHarness,
) -> None:
    h = admin_rbac_harness
    h.harness.configure_fetcher_for(h.norole_persona)
    with patch_validator_for_actor(h.norole_persona):
        resp = await h.client.post(
            f"/v1/admin/tenants/{h.tenant_id}/progression-definitions",
            json={"entity_type": "cap", "definition": _DEFINITION_BODY, "is_advisory": True},
            headers=bearer_headers(tenant_slug=h.tenant_slug),
        )
    assert _denied(resp.status_code), f"expected 401/403, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_list_progression_definitions_requires_admin(
    admin_rbac_harness: _AdminRbacHarness,
) -> None:
    h = admin_rbac_harness
    h.harness.configure_fetcher_for(h.norole_persona)
    with patch_validator_for_actor(h.norole_persona):
        resp = await h.client.get(
            f"/v1/admin/tenants/{h.tenant_id}/progression-definitions",
            headers=bearer_headers(tenant_slug=h.tenant_slug),
        )
    assert _denied(resp.status_code), f"expected 401/403, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_get_progression_definition_by_id_requires_admin(
    admin_rbac_harness: _AdminRbacHarness,
) -> None:
    h = admin_rbac_harness
    h.harness.configure_fetcher_for(h.norole_persona)
    with patch_validator_for_actor(h.norole_persona):
        resp = await h.client.get(
            f"/v1/admin/tenants/{h.tenant_id}/progression-definitions/{h.progression_id}",
            headers=bearer_headers(tenant_slug=h.tenant_slug),
        )
    assert _denied(resp.status_code), f"expected 401/403, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_put_progression_definition_requires_admin(
    admin_rbac_harness: _AdminRbacHarness,
) -> None:
    h = admin_rbac_harness
    h.harness.configure_fetcher_for(h.norole_persona)
    with patch_validator_for_actor(h.norole_persona):
        resp = await h.client.put(
            f"/v1/admin/tenants/{h.tenant_id}/progression-definitions/{h.progression_id}",
            json={"definition": _DEFINITION_BODY, "is_advisory": False},
            headers=bearer_headers(tenant_slug=h.tenant_slug),
        )
    assert _denied(resp.status_code), f"expected 401/403, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_delete_progression_definition_requires_admin(
    admin_rbac_harness: _AdminRbacHarness,
) -> None:
    h = admin_rbac_harness
    h.harness.configure_fetcher_for(h.norole_persona)
    with patch_validator_for_actor(h.norole_persona):
        resp = await h.client.delete(
            f"/v1/admin/tenants/{h.tenant_id}/progression-definitions/{h.progression_id}",
            headers=bearer_headers(tenant_slug=h.tenant_slug),
        )
    assert _denied(resp.status_code), f"expected 401/403, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_post_progression_override_requires_admin(
    admin_rbac_harness: _AdminRbacHarness,
) -> None:
    h = admin_rbac_harness
    h.harness.configure_fetcher_for(h.norole_persona)
    with patch_validator_for_actor(h.norole_persona):
        resp = await h.client.post(
            f"/v1/admin/tenants/{h.tenant_id}/entities/{h.entity_id}/progression-overrides",
            json={
                "from_state": "1",
                "to_state": "2",
                "gate_id": "some-gate",
                "bypass_skip_rules": False,
                "reason": "test",
                "t_valid_to": "2099-12-31T23:59:59Z",
            },
            headers=bearer_headers(tenant_slug=h.tenant_slug),
        )
    assert _denied(resp.status_code), f"expected 401/403, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_list_progression_overrides_requires_admin(
    admin_rbac_harness: _AdminRbacHarness,
) -> None:
    h = admin_rbac_harness
    h.harness.configure_fetcher_for(h.norole_persona)
    with patch_validator_for_actor(h.norole_persona):
        resp = await h.client.get(
            f"/v1/admin/tenants/{h.tenant_id}/entities/{h.entity_id}/progression-overrides",
            headers=bearer_headers(tenant_slug=h.tenant_slug),
        )
    assert _denied(resp.status_code), f"expected 401/403, got {resp.status_code}: {resp.text}"
