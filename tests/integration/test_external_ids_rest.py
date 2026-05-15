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

import uuid
from collections.abc import AsyncIterator
from typing import Any

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

# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_vocabulary(pg_url: str, tenant_slug: str) -> None:
    """Seed minimal vocabulary for a JIT-materialised tenant."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            row = (
                await session.execute(
                    text("SELECT tenant_id FROM tenants WHERE slug = :slug"),
                    {"slug": tenant_slug},
                )
            ).first()
            assert row is not None, f"tenant {tenant_slug} not materialised yet"
            tenant_id = row[0]
            for kind, value in [
                ("entity_type", "capability"),
                ("entity_type", "concept"),
                ("entity_type", "operation"),
                ("fact_category", "overview"),
                ("fact_category", "adr"),
                ("fact_category", "dev_doc"),
                ("edge_rel", "concept_of"),
                ("edge_rel", "operation_of"),
                ("edge_rel", "depends_on"),
                ("edge_rel", "replaced_by"),
            ]:
                await session.execute(
                    text(
                        "INSERT INTO vocabulary_values (tenant_id, kind, value, is_system) "
                        "VALUES (:tid, :kind, :value, FALSE) "
                        "ON CONFLICT DO NOTHING"
                    ),
                    {"tid": tenant_id, "kind": kind, "value": value},
                )
    finally:
        await engine.dispose()


async def _make_persona(
    h: EntitlementAuthHarness, pg_url: str, *, slug: str, roles: list[str]
) -> TenantPersona:
    """Add a persona, materialise the tenant, seed vocab."""
    persona = h.add_persona(slug, roles=roles)
    h.configure_fetcher_for(persona)
    transport = ASGITransport(app=h.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch_validator_for_actor(persona):
            resp = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))
            assert resp.status_code == 200, resp.text
    await _seed_vocabulary(pg_url, slug)
    return persona


async def _seed_entity(pg_url: str, *, tenant_id: uuid.UUID, name: str = "test-cap") -> uuid.UUID:
    """Insert a single capability entity. Returns entity_id."""
    import datetime

    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    entity_id = uuid.uuid4()
    _now = datetime.datetime(2026, 5, 10, 12, 0, 0, tzinfo=datetime.UTC)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO entities "
                    "(entity_id, tenant_id, entity_type, name, is_active, created_at) "
                    "VALUES (:eid, :tid, 'capability', :name, TRUE, :now)"
                ),
                {"eid": entity_id, "tid": tenant_id, "name": name, "now": _now},
            )
    finally:
        await engine.dispose()
    return entity_id


# ---------------------------------------------------------------------------
# Per-test fixture: tenant + entity + http client
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def http_client(pg_container: str) -> AsyncIterator[tuple[AsyncClient, dict[str, Any]]]:
    """Seed a fresh tenant + entity per test; yield (client, setup)."""
    slug = f"t14-{uuid.uuid4().hex[:8]}"
    async with EntitlementAuthHarness(pg_container) as h:
        persona = await _make_persona(h, pg_container, slug=slug, roles=["admin", "producer", "consumer"])

        engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                row = (
                    await session.execute(
                        text("SELECT tenant_id FROM tenants WHERE slug = :slug"), {"slug": slug}
                    )
                ).first()
                assert row is not None
                tenant_id = row[0]
        finally:
            await engine.dispose()

        entity_id = await _seed_entity(pg_container, tenant_id=tenant_id, name="orders-api")
        h.configure_fetcher_for(persona)
        transport = ASGITransport(app=h.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, {
                "pg_url": pg_container,
                "tenant_id": tenant_id,
                "persona": persona,
                "entity_id": entity_id,
                "harness": h,
            }


# ---------------------------------------------------------------------------
# Helper: make authenticated request headers and patch the OIDC validator
# ---------------------------------------------------------------------------


def _auth(setup: dict[str, Any]) -> dict[str, str]:
    return bearer_headers(tenant_slug=setup["persona"].slug)


# ---------------------------------------------------------------------------
# Admin — external systems CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_external_system_201(http_client: Any) -> None:
    """POST /v1/admin/external-systems returns 201 with slug in body."""
    client, setup = http_client
    persona: TenantPersona = setup["persona"]
    harness: EntitlementAuthHarness = setup["harness"]
    harness.configure_fetcher_for(persona)

    with patch_validator_for_actor(persona):
        resp = await client.post(
            "/v1/admin/external-systems",
            json={
                "slug": f"backstage-{uuid.uuid4().hex[:6]}",
                "display_name": "Backstage",
                "url_template": "https://backstage.example.com/registry/{external_id}",
                "description": "Internal developer portal",
            },
            headers=_auth(setup),
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
    persona: TenantPersona = setup["persona"]
    harness: EntitlementAuthHarness = setup["harness"]
    harness.configure_fetcher_for(persona)

    with patch_validator_for_actor(persona):
        resp = await client.post(
            "/v1/admin/external-systems",
            json={"slug": f"plain-{uuid.uuid4().hex[:6]}", "display_name": "Plain"},
            headers=_auth(setup),
        )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["url_template"] is None


@pytest.mark.asyncio
async def test_list_external_systems_200(http_client: Any) -> None:
    """GET /v1/admin/external-systems returns at least the newly registered system."""
    client, setup = http_client
    persona: TenantPersona = setup["persona"]
    harness: EntitlementAuthHarness = setup["harness"]
    harness.configure_fetcher_for(persona)

    slug = f"listable-{uuid.uuid4().hex[:6]}"
    with patch_validator_for_actor(persona):
        await client.post(
            "/v1/admin/external-systems",
            json={"slug": slug, "display_name": "Listable"},
            headers=_auth(setup),
        )
        resp = await client.get("/v1/admin/external-systems", headers=_auth(setup))

    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert isinstance(items, list)
    slugs = [i["slug"] for i in items]
    assert slug in slugs


@pytest.mark.asyncio
async def test_register_duplicate_external_system_409(http_client: Any) -> None:
    """Duplicate slug → 409 Conflict."""
    client, setup = http_client
    persona: TenantPersona = setup["persona"]
    harness: EntitlementAuthHarness = setup["harness"]
    harness.configure_fetcher_for(persona)
    slug = f"dup-{uuid.uuid4().hex[:6]}"

    with patch_validator_for_actor(persona):
        first = await client.post(
            "/v1/admin/external-systems",
            json={"slug": slug, "display_name": "First"},
            headers=_auth(setup),
        )
        assert first.status_code == 201, first.text

        second = await client.post(
            "/v1/admin/external-systems",
            json={"slug": slug, "display_name": "Duplicate"},
            headers=_auth(setup),
        )
    assert second.status_code == 409, (
        f"Duplicate slug should return 409, got {second.status_code}: {second.text}"
    )


@pytest.mark.asyncio
async def test_delete_external_system_204(http_client: Any) -> None:
    """DELETE /v1/admin/external-systems/{slug} returns 204."""
    client, setup = http_client
    persona: TenantPersona = setup["persona"]
    harness: EntitlementAuthHarness = setup["harness"]
    harness.configure_fetcher_for(persona)
    slug = f"todelete-{uuid.uuid4().hex[:6]}"

    with patch_validator_for_actor(persona):
        create_resp = await client.post(
            "/v1/admin/external-systems",
            json={"slug": slug, "display_name": "To Delete"},
            headers=_auth(setup),
        )
        assert create_resp.status_code == 201, create_resp.text

        del_resp = await client.delete(
            f"/v1/admin/external-systems/{slug}",
            headers=_auth(setup),
        )
    assert del_resp.status_code == 204, (
        f"Expected 204 on delete, got {del_resp.status_code}: {del_resp.text}"
    )


@pytest.mark.asyncio
async def test_delete_external_system_not_found_404(http_client: Any) -> None:
    """DELETE on non-existent slug → 404."""
    client, setup = http_client
    persona: TenantPersona = setup["persona"]
    harness: EntitlementAuthHarness = setup["harness"]
    harness.configure_fetcher_for(persona)

    with patch_validator_for_actor(persona):
        resp = await client.delete(
            "/v1/admin/external-systems/ghost-system-does-not-exist",
            headers=_auth(setup),
        )
    assert resp.status_code == 404, (
        f"Expected 404 for missing slug, got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_delete_external_system_post_tunneled(http_client: Any) -> None:
    """POST /v1/admin/external-systems/{slug}:delete (POST-tunneled) returns 204."""
    client, setup = http_client
    persona: TenantPersona = setup["persona"]
    harness: EntitlementAuthHarness = setup["harness"]
    harness.configure_fetcher_for(persona)
    slug = f"tunnel-del-{uuid.uuid4().hex[:6]}"

    with patch_validator_for_actor(persona):
        create_resp = await client.post(
            "/v1/admin/external-systems",
            json={"slug": slug, "display_name": "Tunnel Delete"},
            headers=_auth(setup),
        )
        assert create_resp.status_code == 201, create_resp.text

        tunnel_resp = await client.post(
            f"/v1/admin/external-systems/{slug}:delete",
            headers=_auth(setup),
        )
    assert tunnel_resp.status_code == 204, (
        f"Expected 204 from POST-tunneled delete, got {tunnel_resp.status_code}: {tunnel_resp.text}"
    )


# ---------------------------------------------------------------------------
# Entity mapping endpoints — core contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_external_id_with_url_template_substitution(http_client: Any) -> None:
    """POST /v1/entities/{id}/external-ids returns 201 with url_template substituted."""
    client, setup = http_client
    persona: TenantPersona = setup["persona"]
    harness: EntitlementAuthHarness = setup["harness"]
    harness.configure_fetcher_for(persona)
    entity_id = setup["entity_id"]

    sys_slug = f"jira-{uuid.uuid4().hex[:6]}"
    template = "https://jira.example.com/browse/{external_id}"
    with patch_validator_for_actor(persona):
        reg_resp = await client.post(
            "/v1/admin/external-systems",
            json={"slug": sys_slug, "display_name": "Jira", "url_template": template},
            headers=_auth(setup),
        )
        assert reg_resp.status_code == 201, reg_resp.text

        ext_id = "PROJ-42"
        add_resp = await client.post(
            f"/v1/entities/{entity_id}/external-ids",
            json={"external_system_slug": sys_slug, "external_id": ext_id},
            headers=_auth(setup),
        )
    assert add_resp.status_code == 201, f"Expected 201, got {add_resp.status_code}: {add_resp.text}"

    body = add_resp.json()
    assert body["external_system_slug"] == sys_slug
    assert body["external_id"] == ext_id
    assert body["entity_id"] == str(entity_id)
    expected_url = template.replace("{external_id}", ext_id)
    assert body["url"] == expected_url, f"Expected url={expected_url!r}, got {body['url']!r}"
    assert "external_id_pk" in body


@pytest.mark.asyncio
async def test_list_external_ids_for_entity(http_client: Any) -> None:
    """GET /v1/entities/{entity_id}/external-ids returns the mapping list."""
    client, setup = http_client
    persona: TenantPersona = setup["persona"]
    harness: EntitlementAuthHarness = setup["harness"]
    harness.configure_fetcher_for(persona)
    entity_id = setup["entity_id"]

    sys_slug = f"github-{uuid.uuid4().hex[:6]}"
    ext_id = f"issue-{uuid.uuid4().hex[:6]}"
    with patch_validator_for_actor(persona):
        await client.post(
            "/v1/admin/external-systems",
            json={"slug": sys_slug, "display_name": "GitHub"},
            headers=_auth(setup),
        )
        await client.post(
            f"/v1/entities/{entity_id}/external-ids",
            json={"external_system_slug": sys_slug, "external_id": ext_id},
            headers=_auth(setup),
        )
        list_resp = await client.get(
            f"/v1/entities/{entity_id}/external-ids",
            headers=_auth(setup),
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
    persona: TenantPersona = setup["persona"]
    harness: EntitlementAuthHarness = setup["harness"]
    harness.configure_fetcher_for(persona)
    entity_id = setup["entity_id"]

    sys_slug = f"linear-{uuid.uuid4().hex[:6]}"
    ext_id = f"LIN-{uuid.uuid4().int % 9999}"
    with patch_validator_for_actor(persona):
        await client.post(
            "/v1/admin/external-systems",
            json={"slug": sys_slug, "display_name": "Linear"},
            headers=_auth(setup),
        )
        await client.post(
            f"/v1/entities/{entity_id}/external-ids",
            json={"external_system_slug": sys_slug, "external_id": ext_id},
            headers=_auth(setup),
        )
        lookup_resp = await client.get(
            "/v1/entities",
            params={"external_system": sys_slug, "external_id": ext_id},
            headers=_auth(setup),
        )
    assert lookup_resp.status_code == 200, (
        f"Expected 200 from lookup, got {lookup_resp.status_code}: {lookup_resp.text}"
    )
    body = lookup_resp.json()
    assert body["entity_id"] == str(entity_id), (
        f"Lookup returned wrong entity: expected {entity_id}, got {body['entity_id']}"
    )
    assert "entity_type" in body
    assert "name" in body


@pytest.mark.asyncio
async def test_lookup_entity_not_found_404(http_client: Any) -> None:
    """GET /v1/entities?external_system=&external_id= → 404 for unknown mapping."""
    client, setup = http_client
    persona: TenantPersona = setup["persona"]
    harness: EntitlementAuthHarness = setup["harness"]
    harness.configure_fetcher_for(persona)

    with patch_validator_for_actor(persona):
        resp = await client.get(
            "/v1/entities",
            params={"external_system": "nonexistent-sys", "external_id": "GHOST-999"},
            headers=_auth(setup),
        )
    assert resp.status_code == 404, (
        f"Expected 404 for unknown mapping, got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_duplicate_external_id_returns_409(http_client: Any) -> None:
    """Duplicate (tenant, system_slug, external_id) → 409 Conflict with existing pk in message."""
    client, setup = http_client
    persona: TenantPersona = setup["persona"]
    harness: EntitlementAuthHarness = setup["harness"]
    harness.configure_fetcher_for(persona)
    entity_id = setup["entity_id"]

    sys_slug = f"dup-ext-{uuid.uuid4().hex[:6]}"
    ext_id = f"DUP-{uuid.uuid4().hex[:6]}"
    with patch_validator_for_actor(persona):
        await client.post(
            "/v1/admin/external-systems",
            json={"slug": sys_slug, "display_name": "Dup Ext"},
            headers=_auth(setup),
        )
        first = await client.post(
            f"/v1/entities/{entity_id}/external-ids",
            json={"external_system_slug": sys_slug, "external_id": ext_id},
            headers=_auth(setup),
        )
        assert first.status_code == 201, first.text

        second = await client.post(
            f"/v1/entities/{entity_id}/external-ids",
            json={"external_system_slug": sys_slug, "external_id": ext_id},
            headers=_auth(setup),
        )
    assert second.status_code == 409, (
        f"Duplicate external_id should return 409, got {second.status_code}: {second.text}"
    )
    first_pk = first.json()["external_id_pk"]
    assert first_pk in second.text, f"409 message should cite existing external_id_pk={first_pk}"


@pytest.mark.asyncio
async def test_patch_external_id_updates_url(http_client: Any) -> None:
    """PATCH /v1/entities/{entity_id}/external-ids/{pk} updates the url field."""
    client, setup = http_client
    persona: TenantPersona = setup["persona"]
    harness: EntitlementAuthHarness = setup["harness"]
    harness.configure_fetcher_for(persona)
    entity_id = setup["entity_id"]

    sys_slug = f"patch-sys-{uuid.uuid4().hex[:6]}"
    ext_id = f"PATCH-{uuid.uuid4().hex[:6]}"
    with patch_validator_for_actor(persona):
        await client.post(
            "/v1/admin/external-systems",
            json={"slug": sys_slug, "display_name": "Patch System"},
            headers=_auth(setup),
        )
        add_resp = await client.post(
            f"/v1/entities/{entity_id}/external-ids",
            json={"external_system_slug": sys_slug, "external_id": ext_id},
            headers=_auth(setup),
        )
        assert add_resp.status_code == 201, add_resp.text
        pk = add_resp.json()["external_id_pk"]

        new_url = "https://custom.example.com/patched-link"
        patch_resp = await client.patch(
            f"/v1/entities/{entity_id}/external-ids/{pk}",
            json={"url": new_url},
            headers=_auth(setup),
        )
    assert patch_resp.status_code == 200, (
        f"Expected 200 from PATCH, got {patch_resp.status_code}: {patch_resp.text}"
    )
    assert patch_resp.json()["url"] == new_url


@pytest.mark.asyncio
async def test_delete_external_id_204(http_client: Any) -> None:
    """DELETE /v1/entities/{entity_id}/external-ids/{pk} → 204; mapping disappears from list."""
    client, setup = http_client
    persona: TenantPersona = setup["persona"]
    harness: EntitlementAuthHarness = setup["harness"]
    harness.configure_fetcher_for(persona)
    entity_id = setup["entity_id"]

    sys_slug = f"del-map-{uuid.uuid4().hex[:6]}"
    ext_id = f"DELMAP-{uuid.uuid4().hex[:6]}"
    with patch_validator_for_actor(persona):
        await client.post(
            "/v1/admin/external-systems",
            json={"slug": sys_slug, "display_name": "Del Map"},
            headers=_auth(setup),
        )
        add_resp = await client.post(
            f"/v1/entities/{entity_id}/external-ids",
            json={"external_system_slug": sys_slug, "external_id": ext_id},
            headers=_auth(setup),
        )
        assert add_resp.status_code == 201, add_resp.text
        pk = add_resp.json()["external_id_pk"]

        del_resp = await client.delete(
            f"/v1/entities/{entity_id}/external-ids/{pk}",
            headers=_auth(setup),
        )
        assert del_resp.status_code == 204, (
            f"Expected 204 from DELETE, got {del_resp.status_code}: {del_resp.text}"
        )

        list_resp = await client.get(
            f"/v1/entities/{entity_id}/external-ids",
            headers=_auth(setup),
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
    persona: TenantPersona = setup["persona"]
    harness: EntitlementAuthHarness = setup["harness"]
    harness.configure_fetcher_for(persona)
    entity_id = setup["entity_id"]
    fake_pk = uuid.uuid4()

    with patch_validator_for_actor(persona):
        resp = await client.delete(
            f"/v1/entities/{entity_id}/external-ids/{fake_pk}",
            headers=_auth(setup),
        )
    assert resp.status_code == 404, (
        f"Expected 404 for missing pk, got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_delete_external_id_post_tunneled_204(http_client: Any) -> None:
    """POST /v1/entities/{entity_id}/external-ids/{pk}:delete → 204 (POST-tunneled alias)."""
    client, setup = http_client
    persona: TenantPersona = setup["persona"]
    harness: EntitlementAuthHarness = setup["harness"]
    harness.configure_fetcher_for(persona)
    entity_id = setup["entity_id"]

    sys_slug = f"tunnel-map-{uuid.uuid4().hex[:6]}"
    ext_id = f"TUNNEL-{uuid.uuid4().hex[:6]}"
    with patch_validator_for_actor(persona):
        await client.post(
            "/v1/admin/external-systems",
            json={"slug": sys_slug, "display_name": "Tunnel Map"},
            headers=_auth(setup),
        )
        add_resp = await client.post(
            f"/v1/entities/{entity_id}/external-ids",
            json={"external_system_slug": sys_slug, "external_id": ext_id},
            headers=_auth(setup),
        )
        assert add_resp.status_code == 201, add_resp.text
        pk = add_resp.json()["external_id_pk"]

        tunnel_resp = await client.post(
            f"/v1/entities/{entity_id}/external-ids/{pk}:delete",
            headers=_auth(setup),
        )
    assert tunnel_resp.status_code == 204, (
        f"Expected 204 from POST-tunneled DELETE, got {tunnel_resp.status_code}: {tunnel_resp.text}"
    )


@pytest.mark.asyncio
async def test_add_external_id_unregistered_system_404(http_client: Any) -> None:
    """POST with unregistered external_system_slug → 404."""
    client, setup = http_client
    persona: TenantPersona = setup["persona"]
    harness: EntitlementAuthHarness = setup["harness"]
    harness.configure_fetcher_for(persona)
    entity_id = setup["entity_id"]

    with patch_validator_for_actor(persona):
        resp = await client.post(
            f"/v1/entities/{entity_id}/external-ids",
            json={"external_system_slug": "ghost-system", "external_id": "EXT-1"},
            headers=_auth(setup),
        )
    assert resp.status_code == 404, (
        f"Expected 404 for unregistered system, got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_add_external_id_unknown_entity_404(http_client: Any) -> None:
    """POST to /v1/entities/{entity_id}/external-ids with non-existent entity → 404."""
    client, setup = http_client
    persona: TenantPersona = setup["persona"]
    harness: EntitlementAuthHarness = setup["harness"]
    harness.configure_fetcher_for(persona)

    sys_slug = f"ghost-entity-{uuid.uuid4().hex[:6]}"
    ghost_entity = uuid.uuid4()
    with patch_validator_for_actor(persona):
        await client.post(
            "/v1/admin/external-systems",
            json={"slug": sys_slug, "display_name": "Ghost Entity Test"},
            headers=_auth(setup),
        )
        resp = await client.post(
            f"/v1/entities/{ghost_entity}/external-ids",
            json={"external_system_slug": sys_slug, "external_id": "EXT-1"},
            headers=_auth(setup),
        )
    assert resp.status_code == 404, (
        f"Expected 404 for non-existent entity, got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_unauthenticated_request_401(http_client: Any) -> None:
    """Requests without a valid bearer token return 401."""
    client, setup = http_client
    entity_id = setup["entity_id"]

    resp = await client.get(f"/v1/entities/{entity_id}/external-ids")
    assert resp.status_code == 401, (
        f"Expected 401 for unauthenticated request, got {resp.status_code}"
    )
