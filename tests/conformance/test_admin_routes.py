"""Admin RBAC conformance gate: all progression admin endpoints must return HTTP 403
for callers whose token carries no roles (roles=[]).

This test enumerates the known progression admin paths and asserts that each
returns 403 when called with an unprivileged token. The check is explicit
rather than auto-enumerated from the OpenAPI spec so it stays stable across
spec regeneration cycles and does not require a live DB spec export.

Covered endpoints:
  POST   /v1/admin/tenants/{tenant_id}/progression-definitions
  GET    /v1/admin/tenants/{tenant_id}/progression-definitions
  GET    /v1/admin/tenants/{tenant_id}/progression-definitions/{id}
  PUT    /v1/admin/tenants/{tenant_id}/progression-definitions/{id}
  DELETE /v1/admin/tenants/{tenant_id}/progression-definitions/{id}
  POST   /v1/admin/tenants/{tenant_id}/entities/{entity_id}/progression-overrides
  GET    /v1/admin/tenants/{tenant_id}/entities/{entity_id}/progression-overrides
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

_DEFINITION_BODY = {
    "states": [
        {"id": "draft", "name": "Draft"},
        {"id": "published", "name": "Published"},
    ],
    "transitions": {"forward": "sequential"},
}


# ---------------------------------------------------------------------------
# Fixture: seed a tenant + two tokens (admin vs. no-role)
# ---------------------------------------------------------------------------


async def _seed_tenant_with_tokens(
    pg_url: str,
    *,
    slug: str,
) -> tuple[uuid.UUID, str, str]:
    """Seed one tenant + admin token + no-role token. Returns (tenant_id, admin_token, norole_token)."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    admin_actor_id = uuid.uuid4()
    norole_actor_id = uuid.uuid4()
    admin_raw = secrets.token_urlsafe(24)
    norole_raw = secrets.token_urlsafe(24)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                    "VALUES (:tid, :slug, :slug, :now, TRUE)"
                ),
                {"tid": tenant_id, "slug": slug, "now": _NOW},
            )
            for actor_id, dn in [(admin_actor_id, "admin-actor"), (norole_actor_id, "norole-actor")]:
                await session.execute(
                    text(
                        "INSERT INTO actors (actor_id, tenant_id, display_name, created_at) "
                        "VALUES (:aid, :tid, :dn, :now)"
                    ),
                    {"aid": actor_id, "tid": tenant_id, "dn": dn, "now": _NOW},
                )
            await session.execute(
                text(
                    "INSERT INTO api_tokens "
                    "(token_id, tenant_id, actor_id, token_hash, roles, created_at) "
                    "VALUES (gen_random_uuid(), :tid, :aid, :th, :roles, :now)"
                ),
                {
                    "tid": tenant_id,
                    "aid": admin_actor_id,
                    "th": hash_token(admin_raw),
                    "roles": ["admin"],
                    "now": _NOW,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO api_tokens "
                    "(token_id, tenant_id, actor_id, token_hash, roles, created_at) "
                    "VALUES (gen_random_uuid(), :tid, :aid, :th, :roles, :now)"
                ),
                {
                    "tid": tenant_id,
                    "aid": norole_actor_id,
                    "th": hash_token(norole_raw),
                    "roles": [],
                    "now": _NOW,
                },
            )
    finally:
        await engine.dispose()
    return tenant_id, admin_raw, norole_raw


async def _seed_entity(pg_url: str, *, tenant_id: uuid.UUID) -> uuid.UUID:
    entity_id = uuid.uuid4()
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO entities "
                    "(entity_id, tenant_id, entity_type, name, is_active, created_at) "
                    "VALUES (:eid, :tid, 'capability', :name, TRUE, :now)"
                ),
                {"eid": entity_id, "tid": tenant_id, "name": f"ent-{entity_id}", "now": _NOW},
            )
    finally:
        await engine.dispose()
    return entity_id


# ---------------------------------------------------------------------------
# Shared harness: (tenant_id, entity_id, norole_client, admin_client)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def admin_rbac_harness(pg_container: str, app_settings: Settings):
    """Yield (tenant_id, entity_id, progression_id, norole_client, admin_client)."""
    slug = f"rbac-prog-{secrets.token_hex(4)}"
    tenant_id, admin_token, norole_token = await _seed_tenant_with_tokens(pg_container, slug=slug)
    entity_id = await _seed_entity(pg_container, tenant_id=tenant_id)

    app = create_app(app_settings)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as admin_client:
        async with AsyncClient(transport=transport, base_url="http://test") as norole_client:
            admin_client.headers["Authorization"] = f"Bearer {admin_token}"
            norole_client.headers["Authorization"] = f"Bearer {norole_token}"

            # Create one definition so the GET-by-id and PUT/DELETE paths can
            # receive a real progression_id.
            def_resp = await admin_client.post(
                f"/v1/admin/tenants/{tenant_id}/progression-definitions",
                json={
                    "entity_type": f"et-rbac-{secrets.token_hex(4)}",
                    "definition": _DEFINITION_BODY,
                    "is_advisory": True,
                },
            )
            assert def_resp.status_code == 201, def_resp.text
            progression_id = def_resp.json()["progression_id"]

            yield tenant_id, entity_id, progression_id, norole_client, admin_client


# ---------------------------------------------------------------------------
# 403 for no-role caller — all seven progression endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_progression_definition_requires_admin(admin_rbac_harness) -> None:
    """POST progression-definitions returns 403 for caller with roles=[]."""
    tenant_id, _, _pid, norole_client, _ = admin_rbac_harness
    resp = await norole_client.post(
        f"/v1/admin/tenants/{tenant_id}/progression-definitions",
        json={"entity_type": "cap", "definition": _DEFINITION_BODY, "is_advisory": True},
    )
    assert resp.status_code == 403, f"expected 403, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_list_progression_definitions_requires_admin(admin_rbac_harness) -> None:
    """GET progression-definitions list returns 403 for caller with roles=[]."""
    tenant_id, _, _pid, norole_client, _ = admin_rbac_harness
    resp = await norole_client.get(f"/v1/admin/tenants/{tenant_id}/progression-definitions")
    assert resp.status_code == 403, f"expected 403, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_get_progression_definition_by_id_requires_admin(admin_rbac_harness) -> None:
    """GET progression-definitions/{id} returns 403 for caller with roles=[]."""
    tenant_id, _, progression_id, norole_client, _ = admin_rbac_harness
    resp = await norole_client.get(f"/v1/admin/tenants/{tenant_id}/progression-definitions/{progression_id}")
    assert resp.status_code == 403, f"expected 403, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_put_progression_definition_requires_admin(admin_rbac_harness) -> None:
    """PUT progression-definitions/{id} returns 403 for caller with roles=[]."""
    tenant_id, _, progression_id, norole_client, _ = admin_rbac_harness
    resp = await norole_client.put(
        f"/v1/admin/tenants/{tenant_id}/progression-definitions/{progression_id}",
        json={"definition": _DEFINITION_BODY, "is_advisory": False},
    )
    assert resp.status_code == 403, f"expected 403, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_delete_progression_definition_requires_admin(admin_rbac_harness) -> None:
    """DELETE progression-definitions/{id} returns 403 for caller with roles=[]."""
    tenant_id, _, progression_id, norole_client, _ = admin_rbac_harness
    resp = await norole_client.delete(f"/v1/admin/tenants/{tenant_id}/progression-definitions/{progression_id}")
    assert resp.status_code == 403, f"expected 403, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_post_progression_override_requires_admin(admin_rbac_harness) -> None:
    """POST progression-overrides returns 403 for caller with roles=[]."""
    tenant_id, entity_id, _pid, norole_client, _ = admin_rbac_harness
    resp = await norole_client.post(
        f"/v1/admin/tenants/{tenant_id}/entities/{entity_id}/progression-overrides",
        json={
            "from_state": "1",
            "to_state": "2",
            "gate_id": "some-gate",
            "bypass_skip_rules": False,
            "reason": "test",
            "t_valid_to": "2099-12-31T23:59:59Z",
        },
    )
    assert resp.status_code == 403, f"expected 403, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_list_progression_overrides_requires_admin(admin_rbac_harness) -> None:
    """GET progression-overrides returns 403 for caller with roles=[]."""
    tenant_id, entity_id, _pid, norole_client, _ = admin_rbac_harness
    resp = await norole_client.get(f"/v1/admin/tenants/{tenant_id}/entities/{entity_id}/progression-overrides")
    assert resp.status_code == 403, f"expected 403, got {resp.status_code}: {resp.text}"
