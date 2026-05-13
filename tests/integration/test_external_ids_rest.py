"""Integration tests for External-ID REST endpoints.

Contract under test (External ID registry)
-----------------------------------------------------------------------
1.  Register an external system with a url_template via
    POST /v1/admin/external-systems.
2.  List external systems — registered system appears.
3.  Add a mapping via POST /v1/entities/{entity_id}/external-ids:
    - url_template substitution produces the expected resolved URL.
4.  List mappings via GET /v1/entities/{entity_id}/external-ids.
5.  Lookup entity via GET /v1/entities?external_system=&external_id= → EntityRef.
6.  Duplicate insert → 409 Conflict.
7.  PATCH /v1/entities/{entity_id}/external-ids/{pk} updates url/metadata.
8.  DELETE /v1/entities/{entity_id}/external-ids/{pk} (hard delete) → 204;
    subsequent GET confirms mapping is gone.
9.  POST-tunneled DELETE alias → 204 (if mode=both or post_only).
10. DELETE /v1/admin/external-systems/{slug} → 204.
11. Lookup after delete → 404.
12. POST-tunneled DELETE on external-system → 204.
"""

from __future__ import annotations

import datetime
import secrets
import uuid
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.api.auth.tokens import hash_token
from registry.config import Settings
from registry.main import create_app

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOW = datetime.datetime(2026, 5, 10, 12, 0, 0, tzinfo=datetime.UTC)

# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_tenant(pg_url: str, *, slug: str) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Insert tenant + actor + API token (admin role). Returns (tenant_id, actor_id, raw_token)."""
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
                    "VALUES (gen_random_uuid(), :tid, :aid, :th, "
                    "        ARRAY['admin','producer','consumer'], :now)"
                ),
                {
                    "tid": tenant_id,
                    "aid": actor_id,
                    "th": hash_token(raw_token),
                    "now": _NOW,
                },
            )
    finally:
        await engine.dispose()
    return tenant_id, actor_id, raw_token


async def _seed_entity(pg_url: str, *, tenant_id: uuid.UUID, name: str = "test-cap") -> uuid.UUID:
    """Insert a single capability entity. Returns entity_id."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    entity_id = uuid.uuid4()
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO entities "
                    "(entity_id, tenant_id, entity_type, name, is_active, created_at) "
                    "VALUES (:eid, :tid, 'capability', :name, TRUE, :now)"
                ),
                {"eid": entity_id, "tid": tenant_id, "name": name, "now": _NOW},
            )
    finally:
        await engine.dispose()
    return entity_id


# ---------------------------------------------------------------------------
# Module-scoped fixture: tenant + entity shared across all tests in module
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def ext_id_setup(pg_container: str) -> dict[str, Any]:
    """Seed tenant (admin role) and one entity. Returns setup dict."""
    slug = f"t14-{uuid.uuid4().hex[:8]}"
    tenant_id, actor_id, raw_token = await _seed_tenant(pg_container, slug=slug)
    entity_id = await _seed_entity(pg_container, tenant_id=tenant_id, name="orders-api")
    return {
        "pg_url": pg_container,
        "tenant_id": tenant_id,
        "actor_id": actor_id,
        "raw_token": raw_token,
        "entity_id": entity_id,
    }


@pytest_asyncio.fixture
async def http_client(ext_id_setup: dict[str, Any]):  # type: ignore[type-arg]
    """FastAPI app + httpx AsyncClient for T14 tests."""
    pg_url = ext_id_setup["pg_url"]
    settings = Settings(
        database_url=pg_url,
        pgbouncer_url=pg_url,
        scheduler_jobstore_url=pg_url,
        embedding_model="stub",
        scheduler_use_memory_jobstore=True,
    )
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, ext_id_setup


# ---------------------------------------------------------------------------
# Admin — external systems CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_external_system_201(http_client: Any) -> None:
    """POST /v1/admin/external-systems returns 201 with slug in body."""
    client, setup = http_client
    token = setup["raw_token"]

    resp = await client.post(
        "/v1/admin/external-systems",
        json={
            "slug": f"backstage-{uuid.uuid4().hex[:6]}",
            "display_name": "Backstage",
            "url_template": "https://backstage.example.com/registry/{external_id}",
            "description": "Internal developer portal",
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["display_name"] == "Backstage"
    assert body["url_template"] == "https://backstage.example.com/registry/{external_id}"
    assert body["description"] == "Internal developer portal"
    assert "slug" in body
    assert "tenant_id" in body
    assert "created_at" in body


@pytest.mark.asyncio
async def test_register_external_system_no_template(http_client: Any) -> None:
    """POST /v1/admin/external-systems with no url_template stores None."""
    client, setup = http_client
    token = setup["raw_token"]

    resp = await client.post(
        "/v1/admin/external-systems",
        json={"slug": f"plain-{uuid.uuid4().hex[:6]}", "display_name": "Plain"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["url_template"] is None


@pytest.mark.asyncio
async def test_list_external_systems_200(http_client: Any) -> None:
    """GET /v1/admin/external-systems returns at least the newly registered system."""
    client, setup = http_client
    token = setup["raw_token"]

    # First create one.
    slug = f"listable-{uuid.uuid4().hex[:6]}"
    await client.post(
        "/v1/admin/external-systems",
        json={"slug": slug, "display_name": "Listable"},
        headers={"Authorization": f"Bearer {token}"},
    )

    resp = await client.get(
        "/v1/admin/external-systems",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert isinstance(items, list)
    slugs = [i["slug"] for i in items]
    assert slug in slugs


@pytest.mark.asyncio
async def test_register_duplicate_external_system_409(http_client: Any) -> None:
    """Duplicate slug → 409 Conflict."""
    client, setup = http_client
    token = setup["raw_token"]
    slug = f"dup-{uuid.uuid4().hex[:6]}"

    first = await client.post(
        "/v1/admin/external-systems",
        json={"slug": slug, "display_name": "First"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert first.status_code == 201, first.text

    second = await client.post(
        "/v1/admin/external-systems",
        json={"slug": slug, "display_name": "Duplicate"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert second.status_code == 409, f"Duplicate slug should return 409, got {second.status_code}: {second.text}"


@pytest.mark.asyncio
async def test_delete_external_system_204(http_client: Any) -> None:
    """DELETE /v1/admin/external-systems/{slug} returns 204."""
    client, setup = http_client
    token = setup["raw_token"]
    slug = f"todelete-{uuid.uuid4().hex[:6]}"

    create_resp = await client.post(
        "/v1/admin/external-systems",
        json={"slug": slug, "display_name": "To Delete"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert create_resp.status_code == 201, create_resp.text

    del_resp = await client.delete(
        f"/v1/admin/external-systems/{slug}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert del_resp.status_code == 204, f"Expected 204 on delete, got {del_resp.status_code}: {del_resp.text}"


@pytest.mark.asyncio
async def test_delete_external_system_not_found_404(http_client: Any) -> None:
    """DELETE on non-existent slug → 404."""
    client, setup = http_client
    token = setup["raw_token"]

    resp = await client.delete(
        "/v1/admin/external-systems/ghost-system-does-not-exist",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404, f"Expected 404 for missing slug, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_delete_external_system_post_tunneled(http_client: Any) -> None:
    """POST /v1/admin/external-systems/{slug}:delete (POST-tunneled) returns 204."""
    client, setup = http_client
    token = setup["raw_token"]
    slug = f"tunnel-del-{uuid.uuid4().hex[:6]}"

    create_resp = await client.post(
        "/v1/admin/external-systems",
        json={"slug": slug, "display_name": "Tunnel Delete"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert create_resp.status_code == 201, create_resp.text

    tunnel_resp = await client.post(
        f"/v1/admin/external-systems/{slug}:delete",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert (
        tunnel_resp.status_code == 204
    ), f"Expected 204 from POST-tunneled delete, got {tunnel_resp.status_code}: {tunnel_resp.text}"


# ---------------------------------------------------------------------------
# Entity mapping endpoints — core contract (T14)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_external_id_with_url_template_substitution(http_client: Any) -> None:
    """POST /v1/entities/{id}/external-ids returns 201 with url_template substituted.

    This is the primary contract test: register system with url_template;
    add mapping; assert lookup returns correct entity and url is substituted.
    """
    client, setup = http_client
    token = setup["raw_token"]
    entity_id = setup["entity_id"]

    # Register external system with url_template.
    sys_slug = f"jira-{uuid.uuid4().hex[:6]}"
    template = "https://jira.example.com/browse/{external_id}"
    reg_resp = await client.post(
        "/v1/admin/external-systems",
        json={
            "slug": sys_slug,
            "display_name": "Jira",
            "url_template": template,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert reg_resp.status_code == 201, reg_resp.text

    # Add the mapping.
    ext_id = "PROJ-42"
    add_resp = await client.post(
        f"/v1/entities/{entity_id}/external-ids",
        json={
            "external_system_slug": sys_slug,
            "external_id": ext_id,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert add_resp.status_code == 201, f"Expected 201, got {add_resp.status_code}: {add_resp.text}"

    body = add_resp.json()
    assert body["external_system_slug"] == sys_slug
    assert body["external_id"] == ext_id
    assert body["entity_id"] == str(entity_id)
    # url_template substitution must have happened.
    expected_url = template.replace("{external_id}", ext_id)
    assert body["url"] == expected_url, f"Expected url={expected_url!r}, got {body['url']!r}"
    assert "external_id_pk" in body


@pytest.mark.asyncio
async def test_list_external_ids_for_entity(http_client: Any) -> None:
    """GET /v1/entities/{entity_id}/external-ids returns the mapping list."""
    client, setup = http_client
    token = setup["raw_token"]
    entity_id = setup["entity_id"]

    # Register a fresh system and add a mapping.
    sys_slug = f"github-{uuid.uuid4().hex[:6]}"
    await client.post(
        "/v1/admin/external-systems",
        json={"slug": sys_slug, "display_name": "GitHub"},
        headers={"Authorization": f"Bearer {token}"},
    )
    ext_id = f"issue-{uuid.uuid4().hex[:6]}"
    await client.post(
        f"/v1/entities/{entity_id}/external-ids",
        json={"external_system_slug": sys_slug, "external_id": ext_id},
        headers={"Authorization": f"Bearer {token}"},
    )

    # List all mappings.
    list_resp = await client.get(
        f"/v1/entities/{entity_id}/external-ids",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert list_resp.status_code == 200, list_resp.text
    body = list_resp.json()
    items = body["items"] if isinstance(body, dict) else body
    assert isinstance(items, list)
    ext_ids = [i["external_id"] for i in items]
    assert ext_id in ext_ids


@pytest.mark.asyncio
async def test_lookup_entity_by_external_id(http_client: Any) -> None:
    """GET /v1/entities?external_system=&external_id= returns EntityRef."""
    client, setup = http_client
    token = setup["raw_token"]
    entity_id = setup["entity_id"]

    sys_slug = f"linear-{uuid.uuid4().hex[:6]}"
    await client.post(
        "/v1/admin/external-systems",
        json={"slug": sys_slug, "display_name": "Linear"},
        headers={"Authorization": f"Bearer {token}"},
    )
    ext_id = f"LIN-{uuid.uuid4().int % 9999}"
    await client.post(
        f"/v1/entities/{entity_id}/external-ids",
        json={"external_system_slug": sys_slug, "external_id": ext_id},
        headers={"Authorization": f"Bearer {token}"},
    )

    lookup_resp = await client.get(
        "/v1/entities",
        params={"external_system": sys_slug, "external_id": ext_id},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert (
        lookup_resp.status_code == 200
    ), f"Expected 200 from lookup, got {lookup_resp.status_code}: {lookup_resp.text}"
    body = lookup_resp.json()
    assert body["entity_id"] == str(
        entity_id
    ), f"Lookup returned wrong entity: expected {entity_id}, got {body['entity_id']}"
    assert "entity_type" in body
    assert "name" in body


@pytest.mark.asyncio
async def test_lookup_entity_not_found_404(http_client: Any) -> None:
    """GET /v1/entities?external_system=&external_id= → 404 for unknown mapping."""
    client, setup = http_client
    token = setup["raw_token"]

    resp = await client.get(
        "/v1/entities",
        params={"external_system": "nonexistent-sys", "external_id": "GHOST-999"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404, f"Expected 404 for unknown mapping, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_duplicate_external_id_returns_409(http_client: Any) -> None:
    """Duplicate (tenant, system_slug, external_id) → 409 Conflict with existing pk in message."""
    client, setup = http_client
    token = setup["raw_token"]
    entity_id = setup["entity_id"]

    sys_slug = f"dup-ext-{uuid.uuid4().hex[:6]}"
    await client.post(
        "/v1/admin/external-systems",
        json={"slug": sys_slug, "display_name": "Dup Ext"},
        headers={"Authorization": f"Bearer {token}"},
    )

    ext_id = f"DUP-{uuid.uuid4().hex[:6]}"
    first = await client.post(
        f"/v1/entities/{entity_id}/external-ids",
        json={"external_system_slug": sys_slug, "external_id": ext_id},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert first.status_code == 201, first.text

    second = await client.post(
        f"/v1/entities/{entity_id}/external-ids",
        json={"external_system_slug": sys_slug, "external_id": ext_id},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert (
        second.status_code == 409
    ), f"Duplicate external_id should return 409, got {second.status_code}: {second.text}"
    # The error message should cite the existing external_id_pk.
    first_pk = first.json()["external_id_pk"]
    assert first_pk in second.text, f"409 message should cite existing external_id_pk={first_pk}"


@pytest.mark.asyncio
async def test_patch_external_id_updates_url(http_client: Any) -> None:
    """PATCH /v1/entities/{entity_id}/external-ids/{pk} updates the url field."""
    client, setup = http_client
    token = setup["raw_token"]
    entity_id = setup["entity_id"]

    sys_slug = f"patch-sys-{uuid.uuid4().hex[:6]}"
    await client.post(
        "/v1/admin/external-systems",
        json={"slug": sys_slug, "display_name": "Patch System"},
        headers={"Authorization": f"Bearer {token}"},
    )
    ext_id = f"PATCH-{uuid.uuid4().hex[:6]}"
    add_resp = await client.post(
        f"/v1/entities/{entity_id}/external-ids",
        json={"external_system_slug": sys_slug, "external_id": ext_id},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert add_resp.status_code == 201, add_resp.text
    pk = add_resp.json()["external_id_pk"]

    new_url = "https://custom.example.com/patched-link"
    patch_resp = await client.patch(
        f"/v1/entities/{entity_id}/external-ids/{pk}",
        json={"url": new_url},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert patch_resp.status_code == 200, f"Expected 200 from PATCH, got {patch_resp.status_code}: {patch_resp.text}"
    assert patch_resp.json()["url"] == new_url


@pytest.mark.asyncio
async def test_delete_external_id_204(http_client: Any) -> None:
    """DELETE /v1/entities/{entity_id}/external-ids/{pk} → 204; mapping disappears from list."""
    client, setup = http_client
    token = setup["raw_token"]
    entity_id = setup["entity_id"]

    sys_slug = f"del-map-{uuid.uuid4().hex[:6]}"
    await client.post(
        "/v1/admin/external-systems",
        json={"slug": sys_slug, "display_name": "Del Map"},
        headers={"Authorization": f"Bearer {token}"},
    )
    ext_id = f"DELMAP-{uuid.uuid4().hex[:6]}"
    add_resp = await client.post(
        f"/v1/entities/{entity_id}/external-ids",
        json={"external_system_slug": sys_slug, "external_id": ext_id},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert add_resp.status_code == 201, add_resp.text
    pk = add_resp.json()["external_id_pk"]

    del_resp = await client.delete(
        f"/v1/entities/{entity_id}/external-ids/{pk}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert del_resp.status_code == 204, f"Expected 204 from DELETE, got {del_resp.status_code}: {del_resp.text}"

    # Confirm the mapping no longer appears in the list.
    list_resp = await client.get(
        f"/v1/entities/{entity_id}/external-ids",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert list_resp.status_code == 200, list_resp.text
    body = list_resp.json()
    items = body["items"] if isinstance(body, dict) else body
    pks_in_list = [i["external_id_pk"] for i in items]
    assert pk not in pks_in_list, f"Deleted external_id_pk={pk} still appears in list after hard delete"


@pytest.mark.asyncio
async def test_delete_external_id_not_found_404(http_client: Any) -> None:
    """DELETE on non-existent external_id_pk → 404."""
    client, setup = http_client
    token = setup["raw_token"]
    entity_id = setup["entity_id"]
    fake_pk = uuid.uuid4()

    resp = await client.delete(
        f"/v1/entities/{entity_id}/external-ids/{fake_pk}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404, f"Expected 404 for missing pk, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_delete_external_id_post_tunneled_204(http_client: Any) -> None:
    """POST /v1/entities/{entity_id}/external-ids/{pk}:delete → 204 (POST-tunneled alias)."""
    client, setup = http_client
    token = setup["raw_token"]
    entity_id = setup["entity_id"]

    sys_slug = f"tunnel-map-{uuid.uuid4().hex[:6]}"
    await client.post(
        "/v1/admin/external-systems",
        json={"slug": sys_slug, "display_name": "Tunnel Map"},
        headers={"Authorization": f"Bearer {token}"},
    )
    ext_id = f"TUNNEL-{uuid.uuid4().hex[:6]}"
    add_resp = await client.post(
        f"/v1/entities/{entity_id}/external-ids",
        json={"external_system_slug": sys_slug, "external_id": ext_id},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert add_resp.status_code == 201, add_resp.text
    pk = add_resp.json()["external_id_pk"]

    tunnel_resp = await client.post(
        f"/v1/entities/{entity_id}/external-ids/{pk}:delete",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert (
        tunnel_resp.status_code == 204
    ), f"Expected 204 from POST-tunneled DELETE, got {tunnel_resp.status_code}: {tunnel_resp.text}"


@pytest.mark.asyncio
async def test_add_external_id_unregistered_system_404(http_client: Any) -> None:
    """POST with unregistered external_system_slug → 404."""
    client, setup = http_client
    token = setup["raw_token"]
    entity_id = setup["entity_id"]

    resp = await client.post(
        f"/v1/entities/{entity_id}/external-ids",
        json={"external_system_slug": "ghost-system", "external_id": "EXT-1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404, f"Expected 404 for unregistered system, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_add_external_id_unknown_entity_404(http_client: Any) -> None:
    """POST to /v1/entities/{entity_id}/external-ids with non-existent entity → 404."""
    client, setup = http_client
    token = setup["raw_token"]

    # Register a system first.
    sys_slug = f"ghost-entity-{uuid.uuid4().hex[:6]}"
    await client.post(
        "/v1/admin/external-systems",
        json={"slug": sys_slug, "display_name": "Ghost Entity Test"},
        headers={"Authorization": f"Bearer {token}"},
    )

    ghost_entity = uuid.uuid4()
    resp = await client.post(
        f"/v1/entities/{ghost_entity}/external-ids",
        json={"external_system_slug": sys_slug, "external_id": "EXT-1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404, f"Expected 404 for non-existent entity, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_unauthenticated_request_401(http_client: Any) -> None:
    """Requests without a valid bearer token return 401."""
    client, setup = http_client
    entity_id = setup["entity_id"]

    resp = await client.get(f"/v1/entities/{entity_id}/external-ids")
    assert resp.status_code == 401, f"Expected 401 for unauthenticated request, got {resp.status_code}"
