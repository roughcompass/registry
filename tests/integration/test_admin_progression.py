"""Integration tests for the admin progression-definition CRUD endpoints
and the admin progression-override creation/list endpoints.

Covers (filter "not override"):
  POST   /v1/admin/tenants/{tenant_id}/progression-definitions
  GET    /v1/admin/tenants/{tenant_id}/progression-definitions
  GET    /v1/admin/tenants/{tenant_id}/progression-definitions/{progression_id}
  PUT    /v1/admin/tenants/{tenant_id}/progression-definitions/{progression_id}
  DELETE /v1/admin/tenants/{tenant_id}/progression-definitions/{progression_id}

Covers (filter "override"):
  POST   /v1/admin/tenants/{tenant_id}/entities/{entity_id}/progression-overrides
  GET    /v1/admin/tenants/{tenant_id}/entities/{entity_id}/progression-overrides

Definition scenarios:
  - POST happy path: valid definition → 201, body has progression_id, audit row emitted.
  - POST 422: invalid definition (forward = "explicit-graph") → structured error.
  - POST 403: non-admin role (consumer) → rejected by RBAC.
  - GET list: returns the active definition inserted for the tenant.
  - GET one: returns specific definition; 404 for unknown id.
  - PUT supersession: new row inserted, old row's t_valid_to set.
  - DELETE soft-delete: t_valid_to set on the deleted row, no successor inserted.

Override scenarios:
  - Override creation happy path → 201, override_id in body, audit row written.
  - Audit-before-commit failure → HTTP 500, override row NOT inserted.
  - GET filter active (consumed=false, expired=false) → only active override.
  - GET filter expired (expired=true) → only expired override.
  - GET filter by from_state + to_state → only matching override.
  - Non-admin role → 403.
  - Default TTL: omitting t_valid_to → stored value is approximately now + 1 hour.
"""

from __future__ import annotations

import datetime
import secrets
import uuid
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.storage.models import ProgressionDefinition, ProgressionOverride
from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    TenantPersona,
    bearer_headers,
    patch_validator_for_actor,
)

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)

type _ProgressionClients = tuple[
    AsyncClient, AsyncClient, uuid.UUID, str, TenantPersona, TenantPersona
]

# ---------------------------------------------------------------------------
# A minimal valid progression definition JSONB body (passes meta-schema).
# ---------------------------------------------------------------------------

_VALID_DEFINITION = {
    "states": [
        {"id": "draft", "name": "Draft"},
        {"id": "review", "name": "Review"},
        {"id": "published", "name": "Published"},
    ],
    "transitions": {"forward": "sequential"},
}

# A definition that fails meta-schema validation: "forward" must be "sequential"
# or "any", not an arbitrary string.
_INVALID_DEFINITION = {
    "states": [{"id": "draft", "name": "Draft"}],
    "transitions": {"forward": "explicit-graph"},
}


# ---------------------------------------------------------------------------
# Harness helpers
# ---------------------------------------------------------------------------


async def _make_persona(
    h: EntitlementAuthHarness, pg_url: str, *, slug: str, roles: list[str]
) -> TenantPersona:
    """Add a persona, materialise the tenant via a no-op call."""
    persona = h.add_persona(slug, roles=roles)
    h.configure_fetcher_for(persona)
    transport = ASGITransport(app=h.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch_validator_for_actor(persona):
            resp = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))
            assert resp.status_code == 200, resp.text
    return persona


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def harness(pg_container: str) -> AsyncIterator[EntitlementAuthHarness]:
    """Bring up a registry app + mocked entitlement fetcher."""
    async with EntitlementAuthHarness(pg_container) as h:
        yield h


@pytest_asyncio.fixture
async def progression_clients(
    pg_container: str,
) -> AsyncIterator[_ProgressionClients]:
    """Yield (admin_client, consumer_client, tenant_id, pg_url) for progression tests.

    admin_client    — carries ['admin'] role for the test tenant.
    consumer_client — carries ['consumer'] role only (must be rejected by RBAC).
    """
    slug = f"prog-admin-{secrets.token_hex(4)}"
    async with EntitlementAuthHarness(pg_container) as h:
        admin_persona = await _make_persona(h, pg_container, slug=slug, roles=["admin"])
        # Consumer is a separate actor in the same tenant (different persona slug prefix
        # so the harness doesn't collide, but same tenant row via add_persona overload).
        # We register a second actor inside the same tenant slug.
        consumer_persona = h.add_persona(slug, roles=["consumer"], actor_id=uuid.uuid4())
        h.configure_fetcher_for(consumer_persona)

        transport = ASGITransport(app=h.app)
        async with (
            AsyncClient(transport=transport, base_url="http://test") as admin_client,
            AsyncClient(transport=transport, base_url="http://test") as consumer_client,
        ):
            # Look up the materialised tenant_id from the DB.
            engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
            factory = async_sessionmaker(engine, expire_on_commit=False)
            try:
                async with factory() as session:
                    row = (
                        await session.execute(
                            text("SELECT tenant_id FROM tenants WHERE slug = :slug"),
                            {"slug": slug},
                        )
                    ).first()
                    assert row is not None, f"tenant {slug!r} not materialised"
                    tenant_id: uuid.UUID = row[0]
            finally:
                await engine.dispose()

            yield admin_client, consumer_client, tenant_id, pg_container, admin_persona, consumer_persona


# ---------------------------------------------------------------------------
# POST happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_progression_definition_happy_path(progression_clients: _ProgressionClients) -> None:
    """POST with valid definition returns 201 and includes progression_id in body."""
    admin_client, _, tenant_id, _, admin_persona, _ = progression_clients
    with patch_validator_for_actor(admin_persona):
        resp = await admin_client.post(
            f"/v1/admin/tenants/{tenant_id}/progression-definitions",
            json={
                "entity_type": "capability",
                "definition": _VALID_DEFINITION,
                "is_advisory": True,
            },
            headers=bearer_headers(tenant_slug=admin_persona.slug),
        )
    assert resp.status_code == 201, f"expected 201, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert "progression_id" in body
    assert body["entity_type"] == "capability"
    assert body["is_advisory"] is True
    assert body["t_valid_to"] is None


@pytest.mark.asyncio
async def test_post_progression_definition_audit_emitted(
    progression_clients: _ProgressionClients, pg_container: str
) -> None:
    """POST emits a progression.definition.published audit event."""
    admin_client, _, tenant_id, pg_url, admin_persona, _ = progression_clients
    with patch_validator_for_actor(admin_persona):
        resp = await admin_client.post(
            f"/v1/admin/tenants/{tenant_id}/progression-definitions",
            json={
                "entity_type": "concept",
                "definition": _VALID_DEFINITION,
                "is_advisory": True,
            },
            headers=bearer_headers(tenant_slug=admin_persona.slug),
        )
    assert resp.status_code == 201, resp.text
    progression_id = resp.json()["progression_id"]

    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await session.execute(
                text(
                    "SELECT action, after_jsonb FROM audit_log "
                    "WHERE tenant_id = :tid AND action = 'progression.definition.published' "
                    "AND after_jsonb->>'progression_id' = :pid "
                    "LIMIT 1"
                ),
                {"tid": tenant_id, "pid": progression_id},
            )
            row = result.fetchone()
    finally:
        await engine.dispose()

    assert row is not None, "expected audit_log row for progression.definition.published"
    assert row[0] == "progression.definition.published"


# ---------------------------------------------------------------------------
# POST 422 — invalid definition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_progression_definition_invalid_schema(progression_clients: _ProgressionClients) -> None:
    """POST with invalid definition (forward='explicit-graph') returns 422."""
    admin_client, _, tenant_id, _, admin_persona, _ = progression_clients
    with patch_validator_for_actor(admin_persona):
        resp = await admin_client.post(
            f"/v1/admin/tenants/{tenant_id}/progression-definitions",
            json={
                "entity_type": "capability",
                "definition": _INVALID_DEFINITION,
                "is_advisory": True,
            },
            headers=bearer_headers(tenant_slug=admin_persona.slug),
        )
    assert resp.status_code == 422, f"expected 422, got {resp.status_code}: {resp.text}"
    body_text = resp.text
    assert (
        "meta-schema" in body_text or "forward" in body_text or "explicit-graph" in body_text or "enum" in body_text
    ), f"expected validation error details in body: {body_text}"


# ---------------------------------------------------------------------------
# POST 403 — consumer role rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_progression_definition_consumer_forbidden(progression_clients: _ProgressionClients) -> None:
    """POST with consumer-only role returns 403."""
    _, consumer_client, tenant_id, _, _, consumer_persona = progression_clients
    with patch_validator_for_actor(consumer_persona):
        resp = await consumer_client.post(
            f"/v1/admin/tenants/{tenant_id}/progression-definitions",
            json={
                "entity_type": "capability",
                "definition": _VALID_DEFINITION,
                "is_advisory": True,
            },
            headers=bearer_headers(tenant_slug=consumer_persona.slug),
        )
    assert resp.status_code == 403, f"expected 403, got {resp.status_code}: {resp.text}"


# ---------------------------------------------------------------------------
# GET list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_progression_definitions_includes_created(progression_clients: _ProgressionClients) -> None:
    """GET list returns the active definition after POST."""
    admin_client, _, tenant_id, _, admin_persona, _ = progression_clients
    entity_type = f"et-list-{secrets.token_hex(4)}"

    with patch_validator_for_actor(admin_persona):
        post_resp = await admin_client.post(
            f"/v1/admin/tenants/{tenant_id}/progression-definitions",
            json={
                "entity_type": entity_type,
                "definition": _VALID_DEFINITION,
                "is_advisory": True,
            },
            headers=bearer_headers(tenant_slug=admin_persona.slug),
        )
        assert post_resp.status_code == 201, post_resp.text
        created_id = post_resp.json()["progression_id"]

        list_resp = await admin_client.get(
            f"/v1/admin/tenants/{tenant_id}/progression-definitions",
            headers=bearer_headers(tenant_slug=admin_persona.slug),
        )
    assert list_resp.status_code == 200, list_resp.text
    items = list_resp.json()
    ids = [i["progression_id"] for i in items]
    assert created_id in ids, f"expected {created_id} in list: {ids}"


# ---------------------------------------------------------------------------
# GET one — happy path and 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_progression_definition_by_id(progression_clients: _ProgressionClients) -> None:
    """GET one returns the specific definition."""
    admin_client, _, tenant_id, _, admin_persona, _ = progression_clients
    entity_type = f"et-get-{secrets.token_hex(4)}"

    with patch_validator_for_actor(admin_persona):
        post_resp = await admin_client.post(
            f"/v1/admin/tenants/{tenant_id}/progression-definitions",
            json={
                "entity_type": entity_type,
                "definition": _VALID_DEFINITION,
                "is_advisory": False,
            },
            headers=bearer_headers(tenant_slug=admin_persona.slug),
        )
        assert post_resp.status_code == 201, post_resp.text
        progression_id = post_resp.json()["progression_id"]

        get_resp = await admin_client.get(
            f"/v1/admin/tenants/{tenant_id}/progression-definitions/{progression_id}",
            headers=bearer_headers(tenant_slug=admin_persona.slug),
        )
    assert get_resp.status_code == 200, get_resp.text
    body = get_resp.json()
    assert body["progression_id"] == progression_id
    assert body["entity_type"] == entity_type
    assert body["is_advisory"] is False


@pytest.mark.asyncio
async def test_get_progression_definition_not_found(progression_clients: _ProgressionClients) -> None:
    """GET one returns 404 for a non-existent progression_id."""
    admin_client, _, tenant_id, _, admin_persona, _ = progression_clients
    unknown_id = str(uuid.uuid4())
    with patch_validator_for_actor(admin_persona):
        resp = await admin_client.get(
            f"/v1/admin/tenants/{tenant_id}/progression-definitions/{unknown_id}",
            headers=bearer_headers(tenant_slug=admin_persona.slug),
        )
    assert resp.status_code == 404, f"expected 404, got {resp.status_code}: {resp.text}"


# ---------------------------------------------------------------------------
# PUT supersession
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_supersession_inserts_new_row_and_closes_old(
    progression_clients: _ProgressionClients, pg_container: str
) -> None:
    """PUT inserts a new row and sets t_valid_to on the active row."""
    admin_client, _, tenant_id, pg_url, admin_persona, _ = progression_clients
    entity_type = f"et-sup-{secrets.token_hex(4)}"

    with patch_validator_for_actor(admin_persona):
        post_resp = await admin_client.post(
            f"/v1/admin/tenants/{tenant_id}/progression-definitions",
            json={
                "entity_type": entity_type,
                "definition": _VALID_DEFINITION,
                "is_advisory": True,
            },
            headers=bearer_headers(tenant_slug=admin_persona.slug),
        )
        assert post_resp.status_code == 201, post_resp.text
        original_id = post_resp.json()["progression_id"]

        updated_definition = {
            "states": [
                {"id": "draft", "name": "Draft"},
                {"id": "published", "name": "Published"},
            ],
            "transitions": {"forward": "sequential"},
        }

        put_resp = await admin_client.put(
            f"/v1/admin/tenants/{tenant_id}/progression-definitions/{original_id}",
            json={"definition": updated_definition, "is_advisory": False},
            headers=bearer_headers(tenant_slug=admin_persona.slug),
        )
    assert put_resp.status_code == 200, f"expected 200, got {put_resp.status_code}: {put_resp.text}"
    new_body = put_resp.json()
    new_id = new_body["progression_id"]
    assert new_id != original_id, "PUT must create a new row"
    assert new_body["is_advisory"] is False

    # Verify old row has t_valid_to set.
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            old_row = await session.get(ProgressionDefinition, uuid.UUID(original_id))
            new_row = await session.get(ProgressionDefinition, uuid.UUID(new_id))
    finally:
        await engine.dispose()

    assert old_row is not None
    assert old_row.t_valid_to is not None, "old row must have t_valid_to set after supersession"
    assert new_row is not None
    assert new_row.t_valid_to is None, "new row must be open (t_valid_to = NULL)"


# ---------------------------------------------------------------------------
# DELETE soft-delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_soft_deletes_row(progression_clients: _ProgressionClients, pg_container: str) -> None:
    """DELETE sets t_valid_to on the row without inserting a successor."""
    admin_client, _, tenant_id, pg_url, admin_persona, _ = progression_clients
    entity_type = f"et-del-{secrets.token_hex(4)}"

    with patch_validator_for_actor(admin_persona):
        post_resp = await admin_client.post(
            f"/v1/admin/tenants/{tenant_id}/progression-definitions",
            json={
                "entity_type": entity_type,
                "definition": _VALID_DEFINITION,
                "is_advisory": True,
            },
            headers=bearer_headers(tenant_slug=admin_persona.slug),
        )
        assert post_resp.status_code == 201, post_resp.text
        progression_id = post_resp.json()["progression_id"]

        del_resp = await admin_client.delete(
            f"/v1/admin/tenants/{tenant_id}/progression-definitions/{progression_id}",
            headers=bearer_headers(tenant_slug=admin_persona.slug),
        )
    assert del_resp.status_code == 204, f"expected 204, got {del_resp.status_code}: {del_resp.text}"

    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            row = await session.get(ProgressionDefinition, uuid.UUID(progression_id))
    finally:
        await engine.dispose()

    assert row is not None
    assert row.t_valid_to is not None, "soft-delete must set t_valid_to"
    assert row.t_invalidated_at is None, "soft-delete must leave t_invalidated_at as NULL"

    # Confirm no successor row was inserted for this entity_type.
    engine2 = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory2 = async_sessionmaker(engine2, expire_on_commit=False)
    try:
        async with factory2() as session:
            result = await session.execute(
                select(ProgressionDefinition).where(
                    ProgressionDefinition.tenant_id == tenant_id,
                    ProgressionDefinition.entity_type == entity_type,
                    ProgressionDefinition.t_valid_to.is_(None),
                )
            )
            active_after = result.scalars().all()
    finally:
        await engine2.dispose()

    assert len(active_after) == 0, "no active row should exist after soft-delete without successor"


@pytest.mark.asyncio
async def test_delete_soft_delete_audit_emitted(progression_clients: _ProgressionClients, pg_container: str) -> None:
    """DELETE emits a progression.definition.soft_deleted audit event."""
    admin_client, _, tenant_id, pg_url, admin_persona, _ = progression_clients
    entity_type = f"et-delaudit-{secrets.token_hex(4)}"

    with patch_validator_for_actor(admin_persona):
        post_resp = await admin_client.post(
            f"/v1/admin/tenants/{tenant_id}/progression-definitions",
            json={
                "entity_type": entity_type,
                "definition": _VALID_DEFINITION,
                "is_advisory": True,
            },
            headers=bearer_headers(tenant_slug=admin_persona.slug),
        )
        assert post_resp.status_code == 201, post_resp.text
        progression_id = post_resp.json()["progression_id"]

        del_resp = await admin_client.delete(
            f"/v1/admin/tenants/{tenant_id}/progression-definitions/{progression_id}",
            headers=bearer_headers(tenant_slug=admin_persona.slug),
        )
    assert del_resp.status_code == 204, del_resp.text

    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await session.execute(
                text(
                    "SELECT action FROM audit_log "
                    "WHERE tenant_id = :tid AND action = 'progression.definition.soft_deleted' "
                    "AND after_jsonb->>'progression_id' = :pid "
                    "LIMIT 1"
                ),
                {"tid": tenant_id, "pid": progression_id},
            )
            audit_row = result.fetchone()
    finally:
        await engine.dispose()

    assert audit_row is not None, "expected audit_log row for progression.definition.soft_deleted"


# ---------------------------------------------------------------------------
# Override helpers + fixtures
# ---------------------------------------------------------------------------


async def _seed_entity(pg_url: str, *, tenant_id: uuid.UUID) -> uuid.UUID:
    """Insert a minimal entity row and return its entity_id."""
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


_OVERRIDE_PAYLOAD = {
    "from_state": "3",
    "to_state": "5",
    "gate_id": "arb-approved",
    "bypass_skip_rules": False,
    "reason": "Architecture exception approved by CTO",
    "t_valid_to": "2099-12-31T23:59:59Z",
}


# ---------------------------------------------------------------------------
# Override creation — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_override_creation_happy_path(progression_clients: _ProgressionClients) -> None:
    """POST override with valid payload returns 201 with override_id; row and audit row exist."""
    admin_client, _, tenant_id, pg_url, admin_persona, _ = progression_clients
    entity_id = await _seed_entity(pg_url, tenant_id=tenant_id)

    with patch_validator_for_actor(admin_persona):
        resp = await admin_client.post(
            f"/v1/admin/tenants/{tenant_id}/entities/{entity_id}/progression-overrides",
            json=_OVERRIDE_PAYLOAD,
            headers=bearer_headers(tenant_slug=admin_persona.slug),
        )
    assert resp.status_code == 201, f"expected 201, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert "override_id" in body
    override_id = body["override_id"]
    assert body["from_state"] == "3"
    assert body["to_state"] == "5"
    assert body["gate_id"] == "arb-approved"
    assert body["consumed_at"] is None

    # Verify override row persisted.
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            row = await session.get(ProgressionOverride, uuid.UUID(override_id))
    finally:
        await engine.dispose()
    assert row is not None, "override row must exist in DB"
    assert row.tenant_id == tenant_id
    assert row.entity_id == entity_id

    # Verify audit event row exists.
    engine2 = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory2 = async_sessionmaker(engine2, expire_on_commit=False)
    try:
        async with factory2() as session:
            result = await session.execute(
                text(
                    "SELECT action, after_jsonb FROM audit_log "
                    "WHERE tenant_id = :tid AND action = 'progression.override.created' "
                    "AND after_jsonb->>'override_id' = :oid "
                    "LIMIT 1"
                ),
                {"tid": tenant_id, "oid": override_id},
            )
            audit_row = result.fetchone()
    finally:
        await engine2.dispose()
    assert audit_row is not None, "audit_log row must exist for progression.override.created"
    assert audit_row[0] == "progression.override.created"


# ---------------------------------------------------------------------------
# Override creation — audit-before-commit failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_override_creation_audit_before_commit_failure(progression_clients: _ProgressionClients) -> None:
    """When the audit write raises, the override row is NOT created and HTTP 500 is returned."""
    admin_client, _, tenant_id, pg_url, admin_persona, _ = progression_clients
    entity_id = await _seed_entity(pg_url, tenant_id=tenant_id)

    with patch(
        "registry.api.routers.admin_progression._emit_override_audit",
        new_callable=AsyncMock,
        side_effect=Exception("simulated audit write failure"),
    ):
        with patch_validator_for_actor(admin_persona):
            resp = await admin_client.post(
                f"/v1/admin/tenants/{tenant_id}/entities/{entity_id}/progression-overrides",
                json=_OVERRIDE_PAYLOAD,
                headers=bearer_headers(tenant_slug=admin_persona.slug),
            )

    assert resp.status_code == 500, f"expected 500, got {resp.status_code}: {resp.text}"

    # Confirm no override row was inserted.
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await session.execute(
                select(ProgressionOverride).where(
                    ProgressionOverride.tenant_id == tenant_id,
                    ProgressionOverride.entity_id == entity_id,
                )
            )
            rows = result.scalars().all()
    finally:
        await engine.dispose()
    assert len(rows) == 0, "no override row must exist when audit write failed"


# ---------------------------------------------------------------------------
# Override list helpers
# ---------------------------------------------------------------------------


async def _fetch_admin_actor_id(pg_url: str, tenant_id: uuid.UUID) -> uuid.UUID:
    """Return any actor_id seeded under tenant_id."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await session.execute(
                text("SELECT actor_id FROM actors WHERE tenant_id = :tid LIMIT 1"),
                {"tid": tenant_id},
            )
            row = result.fetchone()
    finally:
        await engine.dispose()
    assert row is not None, "expected at least one actor for tenant"
    return uuid.UUID(str(row[0]))


async def _seed_override_row(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    entity_id: uuid.UUID,
    actor_id: uuid.UUID,
    gate_id: str,
    t_valid_to: datetime.datetime,
    consumed_at: datetime.datetime | None,
    now: datetime.datetime,
) -> uuid.UUID:
    """Insert an audit_log row and a progression_override row directly. Returns override_id."""
    override_id = uuid.uuid4()
    audit_id = uuid.uuid4()
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO audit_log "
                    "(audit_id, tenant_id, actor_id, action, target_type, target_id, "
                    " before_jsonb, after_jsonb, ts, request_id, error_code) "
                    "VALUES (:aid, :tid, :actor, 'progression.override.created', "
                    "        'progression_override', :eid, NULL, '{}'::jsonb, :ts, NULL, NULL)"
                ),
                {"aid": audit_id, "tid": tenant_id, "actor": actor_id, "eid": entity_id, "ts": now},
            )
            await session.execute(
                text(
                    "INSERT INTO progression_overrides "
                    "(override_id, tenant_id, entity_id, from_state, to_state, gate_id, "
                    " bypass_skip_rules, reason, authorized_by, t_valid_from, t_valid_to, "
                    " consumed_at, audit_event_id) "
                    "VALUES (:oid, :tid, :eid, '3', '5', :gate, FALSE, 'test', "
                    "        :actor, :now, :tvto, :cat, :aid)"
                ),
                {
                    "oid": override_id,
                    "tid": tenant_id,
                    "eid": entity_id,
                    "gate": gate_id,
                    "actor": actor_id,
                    "now": now,
                    "tvto": t_valid_to,
                    "cat": consumed_at,
                    "aid": audit_id,
                },
            )
    finally:
        await engine.dispose()
    return override_id


# ---------------------------------------------------------------------------
# Override list — filter active (consumed=false, expired=false)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_override_list_filter_active(progression_clients: _ProgressionClients) -> None:
    """GET with consumed=false&expired=false returns only the unconsumed, unexpired override."""
    admin_client, _, tenant_id, pg_url, admin_persona, _ = progression_clients
    entity_id = await _seed_entity(pg_url, tenant_id=tenant_id)
    actor_id = await _fetch_admin_actor_id(pg_url, tenant_id)

    now = datetime.datetime.now(tz=datetime.UTC)

    with patch_validator_for_actor(admin_persona):
        active_resp = await admin_client.post(
            f"/v1/admin/tenants/{tenant_id}/entities/{entity_id}/progression-overrides",
            json={**_OVERRIDE_PAYLOAD, "t_valid_to": "2099-12-31T23:59:59Z", "gate_id": "active-gate"},
            headers=bearer_headers(tenant_slug=admin_persona.slug),
        )
    assert active_resp.status_code == 201, active_resp.text
    active_id = active_resp.json()["override_id"]

    # Expired override — insert directly with t_valid_to in the past.
    expired_id = await _seed_override_row(
        pg_url,
        tenant_id=tenant_id,
        entity_id=entity_id,
        actor_id=actor_id,
        gate_id="expired-gate",
        t_valid_to=now - datetime.timedelta(hours=2),
        consumed_at=None,
        now=now,
    )

    # Consumed override — insert directly with consumed_at set.
    consumed_id = await _seed_override_row(
        pg_url,
        tenant_id=tenant_id,
        entity_id=entity_id,
        actor_id=actor_id,
        gate_id="consumed-gate",
        t_valid_to=now + datetime.timedelta(hours=2),
        consumed_at=now,
        now=now,
    )

    with patch_validator_for_actor(admin_persona):
        list_resp = await admin_client.get(
            f"/v1/admin/tenants/{tenant_id}/entities/{entity_id}/progression-overrides",
            params={"consumed": "false", "expired": "false"},
            headers=bearer_headers(tenant_slug=admin_persona.slug),
        )
    assert list_resp.status_code == 200, list_resp.text
    items = list_resp.json()
    ids = [i["override_id"] for i in items]
    assert active_id in ids, f"active override must appear: {ids}"
    assert str(expired_id) not in ids, "expired override must be excluded"
    assert str(consumed_id) not in ids, "consumed override must be excluded"


# ---------------------------------------------------------------------------
# Override list — filter expired
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_override_list_filter_expired(progression_clients: _ProgressionClients) -> None:
    """GET with expired=true returns only overrides where t_valid_to < now."""
    admin_client, _, tenant_id, pg_url, admin_persona, _ = progression_clients
    entity_id = await _seed_entity(pg_url, tenant_id=tenant_id)
    actor_id = await _fetch_admin_actor_id(pg_url, tenant_id)

    now = datetime.datetime.now(tz=datetime.UTC)

    with patch_validator_for_actor(admin_persona):
        active_resp = await admin_client.post(
            f"/v1/admin/tenants/{tenant_id}/entities/{entity_id}/progression-overrides",
            json={**_OVERRIDE_PAYLOAD, "t_valid_to": "2099-12-31T23:59:59Z", "gate_id": "not-expired"},
            headers=bearer_headers(tenant_slug=admin_persona.slug),
        )
    assert active_resp.status_code == 201, active_resp.text

    expired_id = await _seed_override_row(
        pg_url,
        tenant_id=tenant_id,
        entity_id=entity_id,
        actor_id=actor_id,
        gate_id="expired-only",
        t_valid_to=now - datetime.timedelta(hours=2),
        consumed_at=None,
        now=now,
    )

    with patch_validator_for_actor(admin_persona):
        list_resp = await admin_client.get(
            f"/v1/admin/tenants/{tenant_id}/entities/{entity_id}/progression-overrides",
            params={"expired": "true"},
            headers=bearer_headers(tenant_slug=admin_persona.slug),
        )
    assert list_resp.status_code == 200, list_resp.text
    items = list_resp.json()
    ids = [i["override_id"] for i in items]
    assert str(expired_id) in ids, f"expired override must appear: {ids}"
    assert all(
        i["override_id"] != active_resp.json()["override_id"] for i in items
    ), "active (unexpired) override must not appear in expired=true results"


# ---------------------------------------------------------------------------
# Override list — filter by from_state and to_state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_override_list_filter_by_state(progression_clients: _ProgressionClients) -> None:
    """GET with from_state=3&to_state=5 returns only matching overrides."""
    admin_client, _, tenant_id, pg_url, admin_persona, _ = progression_clients
    entity_id = await _seed_entity(pg_url, tenant_id=tenant_id)

    with patch_validator_for_actor(admin_persona):
        match_resp = await admin_client.post(
            f"/v1/admin/tenants/{tenant_id}/entities/{entity_id}/progression-overrides",
            json={**_OVERRIDE_PAYLOAD, "from_state": "3", "to_state": "5", "gate_id": "match-gate"},
            headers=bearer_headers(tenant_slug=admin_persona.slug),
        )
        assert match_resp.status_code == 201, match_resp.text
        match_id = match_resp.json()["override_id"]

        other_resp = await admin_client.post(
            f"/v1/admin/tenants/{tenant_id}/entities/{entity_id}/progression-overrides",
            json={**_OVERRIDE_PAYLOAD, "from_state": "1", "to_state": "2", "gate_id": "other-gate"},
            headers=bearer_headers(tenant_slug=admin_persona.slug),
        )
        assert other_resp.status_code == 201, other_resp.text
        other_id = other_resp.json()["override_id"]

        list_resp = await admin_client.get(
            f"/v1/admin/tenants/{tenant_id}/entities/{entity_id}/progression-overrides",
            params={"from_state": "3", "to_state": "5"},
            headers=bearer_headers(tenant_slug=admin_persona.slug),
        )
    assert list_resp.status_code == 200, list_resp.text
    items = list_resp.json()
    ids = [i["override_id"] for i in items]
    assert match_id in ids, f"matching override must appear: {ids}"
    assert other_id not in ids, f"non-matching override must be excluded: {ids}"


# ---------------------------------------------------------------------------
# Override creation — non-admin role rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_override_creation_non_admin_role_rejected(progression_clients: _ProgressionClients) -> None:
    """POST override with consumer-only role returns 403."""
    _, consumer_client, tenant_id, pg_url, _, consumer_persona = progression_clients
    entity_id = await _seed_entity(pg_url, tenant_id=tenant_id)

    with patch_validator_for_actor(consumer_persona):
        resp = await consumer_client.post(
            f"/v1/admin/tenants/{tenant_id}/entities/{entity_id}/progression-overrides",
            json=_OVERRIDE_PAYLOAD,
            headers=bearer_headers(tenant_slug=consumer_persona.slug),
        )
    assert resp.status_code == 403, f"expected 403, got {resp.status_code}: {resp.text}"


# ---------------------------------------------------------------------------
# Override creation — default TTL is approximately now + 1 hour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_override_default_ttl_one_hour(progression_clients: _ProgressionClients) -> None:
    """POST without t_valid_to stores t_valid_to within 5 seconds of now + 1 hour."""
    admin_client, _, tenant_id, pg_url, admin_persona, _ = progression_clients
    entity_id = await _seed_entity(pg_url, tenant_id=tenant_id)

    before = datetime.datetime.now(tz=datetime.UTC)
    payload = {k: v for k, v in _OVERRIDE_PAYLOAD.items() if k != "t_valid_to"}
    with patch_validator_for_actor(admin_persona):
        resp = await admin_client.post(
            f"/v1/admin/tenants/{tenant_id}/entities/{entity_id}/progression-overrides",
            json=payload,
            headers=bearer_headers(tenant_slug=admin_persona.slug),
        )
    after = datetime.datetime.now(tz=datetime.UTC)
    assert resp.status_code == 201, f"expected 201, got {resp.status_code}: {resp.text}"
    override_id = resp.json()["override_id"]

    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            row = await session.get(ProgressionOverride, uuid.UUID(override_id))
    finally:
        await engine.dispose()

    assert row is not None
    expected_low = before + datetime.timedelta(hours=1) - datetime.timedelta(seconds=5)
    expected_high = after + datetime.timedelta(hours=1) + datetime.timedelta(seconds=5)
    assert expected_low <= row.t_valid_to <= expected_high, (
        f"t_valid_to {row.t_valid_to} is not approximately now + 1 hour "
        f"(expected between {expected_low} and {expected_high})"
    )


# ---------------------------------------------------------------------------
# Pre-flight graduation helpers
# ---------------------------------------------------------------------------


async def _seed_entity_with_stage(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    entity_type: str,
    stage_progression: str | None,
) -> uuid.UUID:
    """Insert an entity with an optional stage_progression attribute. Returns entity_id."""
    entity_id = uuid.uuid4()
    now = datetime.datetime.now(tz=datetime.UTC)
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO entities "
                    "(entity_id, tenant_id, entity_type, name, is_active, created_at) "
                    "VALUES (:eid, :tid, :etype, :name, TRUE, :now)"
                ),
                {"eid": entity_id, "tid": tenant_id, "etype": entity_type, "name": f"ent-{entity_id}", "now": now},
            )
            if stage_progression is not None:
                attr_id = uuid.uuid4()
                await session.execute(
                    text(
                        "INSERT INTO attributes "
                        "(attr_id, tenant_id, entity_id, key, value, "
                        " t_valid_from, t_valid_to, t_ingested_at, t_invalidated_at) "
                        "VALUES (:aid, :tid, :eid, 'stage_progression', "
                        "        CAST(:val AS jsonb), :now, NULL, :now, NULL)"
                    ),
                    {"aid": attr_id, "tid": tenant_id, "eid": entity_id, "val": f'"{stage_progression}"', "now": now},
                )
    finally:
        await engine.dispose()
    return entity_id


_ADVISORY_DEFINITION = {
    "states": [
        {"id": "draft", "name": "Draft"},
        {"id": "review", "name": "Review"},
        {"id": "published", "name": "Published"},
    ],
    "transitions": {"forward": "sequential"},
}

_ENFORCING_DEFINITION = {
    "states": [
        {"id": "draft", "name": "Draft"},
        {"id": "published", "name": "Published"},
    ],
    "transitions": {"forward": "sequential"},
}


async def _create_advisory_definition(
    admin_client: AsyncClient,
    tenant_id: uuid.UUID,
    entity_type: str,
    admin_persona: TenantPersona,
) -> str:
    """POST an advisory definition; return its progression_id."""
    with patch_validator_for_actor(admin_persona):
        resp = await admin_client.post(
            f"/v1/admin/tenants/{tenant_id}/progression-definitions",
            json={
                "entity_type": entity_type,
                "definition": _ADVISORY_DEFINITION,
                "is_advisory": True,
            },
            headers=bearer_headers(tenant_slug=admin_persona.slug),
        )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["progression_id"])


# ---------------------------------------------------------------------------
# Pre-flight: dry_run returns offenders without writing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_dry_run_returns_offenders_no_write(progression_clients: _ProgressionClients) -> None:
    """PUT with dry_run=true returns 200 + offender list; no new definition row written."""
    admin_client, _, tenant_id, pg_url, admin_persona, _ = progression_clients
    entity_type = f"et-drrun-{secrets.token_hex(4)}"

    progression_id = await _create_advisory_definition(admin_client, tenant_id, entity_type, admin_persona)

    await _seed_entity_with_stage(pg_url, tenant_id=tenant_id, entity_type=entity_type, stage_progression="draft")
    await _seed_entity_with_stage(pg_url, tenant_id=tenant_id, entity_type=entity_type, stage_progression="published")
    offender_id = await _seed_entity_with_stage(
        pg_url, tenant_id=tenant_id, entity_type=entity_type, stage_progression="review"
    )

    with patch_validator_for_actor(admin_persona):
        resp = await admin_client.put(
            f"/v1/admin/tenants/{tenant_id}/progression-definitions/{progression_id}",
            json={
                "definition": _ENFORCING_DEFINITION,
                "is_advisory": False,
                "dry_run": True,
            },
            headers=bearer_headers(tenant_slug=admin_persona.slug),
        )
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body.get("dry_run") is True, f"expected dry_run=true in body: {body}"
    offenders = body.get("offenders", [])
    assert len(offenders) == 1, f"expected exactly 1 offender, got {offenders}"
    assert offenders[0]["entity_id"] == str(offender_id)
    assert offenders[0]["current_state"] == "review"

    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await session.execute(
                select(ProgressionDefinition).where(
                    ProgressionDefinition.tenant_id == tenant_id,
                    ProgressionDefinition.entity_type == entity_type,
                    ProgressionDefinition.is_advisory.is_(False),
                )
            )
            enforcing_rows = result.scalars().all()
    finally:
        await engine.dispose()
    assert len(enforcing_rows) == 0, "dry_run must not write a new enforcing definition row"


# ---------------------------------------------------------------------------
# Pre-flight: force_timeout_seconds exceeded returns 409 with partial list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_timeout_returns_partial(progression_clients: _ProgressionClients) -> None:
    """PUT with force_timeout_seconds=1 and a slow scan returns 409 preflight_timeout."""
    admin_client, _, tenant_id, pg_url, admin_persona, _ = progression_clients
    entity_type = f"et-tmout-{secrets.token_hex(4)}"

    progression_id = await _create_advisory_definition(admin_client, tenant_id, entity_type, admin_persona)

    for _ in range(5):
        await _seed_entity_with_stage(
            pg_url, tenant_id=tenant_id, entity_type=entity_type, stage_progression="draft"
        )

    async def _slow_wait_for(coro, timeout):  # type: ignore[no-untyped-def]
        coro.close()
        raise TimeoutError

    with patch("registry.api.routers.admin_progression.asyncio.wait_for", side_effect=_slow_wait_for):
        with patch_validator_for_actor(admin_persona):
            resp = await admin_client.put(
                f"/v1/admin/tenants/{tenant_id}/progression-definitions/{progression_id}",
                json={
                    "definition": _ENFORCING_DEFINITION,
                    "is_advisory": False,
                    "force_timeout_seconds": 1,
                },
                headers=bearer_headers(tenant_slug=admin_persona.slug),
            )

    assert resp.status_code == 409, f"expected 409, got {resp.status_code}: {resp.text}"
    body = resp.json()
    error_item = body.get("errors", [{}])[0]
    assert error_item.get("code") == "preflight_timeout", f"unexpected body: {body}"

    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await session.execute(
                select(ProgressionDefinition).where(
                    ProgressionDefinition.tenant_id == tenant_id,
                    ProgressionDefinition.entity_type == entity_type,
                    ProgressionDefinition.is_advisory.is_(False),
                )
            )
            enforcing_rows = result.scalars().all()
    finally:
        await engine.dispose()
    assert len(enforcing_rows) == 0, "timeout path must not write a new enforcing definition row"


# ---------------------------------------------------------------------------
# Pre-flight: force=true + migration_plan bypasses scan and records plan in audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_force_with_migration_plan_bypasses_scan(progression_clients: _ProgressionClients) -> None:
    """PUT with force=true and migration_plan writes definition and records plan in audit."""
    admin_client, _, tenant_id, pg_url, admin_persona, _ = progression_clients
    entity_type = f"et-force-{secrets.token_hex(4)}"

    progression_id = await _create_advisory_definition(admin_client, tenant_id, entity_type, admin_persona)

    await _seed_entity_with_stage(pg_url, tenant_id=tenant_id, entity_type=entity_type, stage_progression="review")

    migration_plan_text = "Approved exception 2026-05-12 by CTO"
    with patch_validator_for_actor(admin_persona):
        resp = await admin_client.put(
            f"/v1/admin/tenants/{tenant_id}/progression-definitions/{progression_id}",
            json={
                "definition": _ENFORCING_DEFINITION,
                "is_advisory": False,
                "force": True,
                "migration_plan": migration_plan_text,
            },
            headers=bearer_headers(tenant_slug=admin_persona.slug),
        )
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    new_id = resp.json()["progression_id"]

    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            new_row = await session.get(ProgressionDefinition, uuid.UUID(new_id))
            result = await session.execute(
                text(
                    "SELECT after_jsonb FROM audit_log "
                    "WHERE tenant_id = :tid AND action = 'progression.definition.published' "
                    "AND after_jsonb->>'progression_id' = :pid "
                    "LIMIT 1"
                ),
                {"tid": tenant_id, "pid": new_id},
            )
            audit_row = result.fetchone()
    finally:
        await engine.dispose()

    assert new_row is not None, "new definition row must exist after force bypass"
    assert new_row.is_advisory is False
    assert audit_row is not None, "audit row must exist"
    audit_payload = audit_row[0]
    assert (
        audit_payload.get("migration_plan") == migration_plan_text
    ), f"migration_plan must be in audit payload: {audit_payload}"


# ---------------------------------------------------------------------------
# Pre-flight: force=true without migration_plan is rejected with 400
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_force_without_migration_plan_rejected(progression_clients: _ProgressionClients) -> None:
    """PUT with force=true but no migration_plan returns 400 migration_plan_required."""
    admin_client, _, tenant_id, pg_url, admin_persona, _ = progression_clients
    entity_type = f"et-nomig-{secrets.token_hex(4)}"

    progression_id = await _create_advisory_definition(admin_client, tenant_id, entity_type, admin_persona)

    with patch_validator_for_actor(admin_persona):
        resp = await admin_client.put(
            f"/v1/admin/tenants/{tenant_id}/progression-definitions/{progression_id}",
            json={
                "definition": _ENFORCING_DEFINITION,
                "is_advisory": False,
                "force": True,
            },
            headers=bearer_headers(tenant_slug=admin_persona.slug),
        )
    assert resp.status_code == 400, f"expected 400, got {resp.status_code}: {resp.text}"
    body = resp.json()
    error_item = body.get("errors", [{}])[0]
    assert error_item.get("code") == "migration_plan_required", f"unexpected body: {body}"


# ---------------------------------------------------------------------------
# Pre-flight: zero offenders — clean graduation writes definition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_clean_graduation_writes_definition(progression_clients: _ProgressionClients) -> None:
    """Zero offenders with dry_run=false, force=false results in definition being written."""
    admin_client, _, tenant_id, pg_url, admin_persona, _ = progression_clients
    entity_type = f"et-clean-{secrets.token_hex(4)}"

    progression_id = await _create_advisory_definition(admin_client, tenant_id, entity_type, admin_persona)

    await _seed_entity_with_stage(pg_url, tenant_id=tenant_id, entity_type=entity_type, stage_progression="draft")
    await _seed_entity_with_stage(pg_url, tenant_id=tenant_id, entity_type=entity_type, stage_progression="published")

    with patch_validator_for_actor(admin_persona):
        resp = await admin_client.put(
            f"/v1/admin/tenants/{tenant_id}/progression-definitions/{progression_id}",
            json={
                "definition": _ENFORCING_DEFINITION,
                "is_advisory": False,
                "dry_run": False,
                "force": False,
            },
            headers=bearer_headers(tenant_slug=admin_persona.slug),
        )
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    new_id = resp.json()["progression_id"]

    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            new_row = await session.get(ProgressionDefinition, uuid.UUID(new_id))
    finally:
        await engine.dispose()
    assert new_row is not None, "definition row must exist after clean graduation"
    assert new_row.is_advisory is False


# ---------------------------------------------------------------------------
# Pre-flight: offenders present with force=false returns 409
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_offenders_present_force_false_rejected(progression_clients: _ProgressionClients) -> None:
    """Two offenders + force=false returns 409 preflight_offenders_present with offender list."""
    admin_client, _, tenant_id, pg_url, admin_persona, _ = progression_clients
    entity_type = f"et-offend-{secrets.token_hex(4)}"

    progression_id = await _create_advisory_definition(admin_client, tenant_id, entity_type, admin_persona)

    offender1 = await _seed_entity_with_stage(
        pg_url, tenant_id=tenant_id, entity_type=entity_type, stage_progression="review"
    )
    offender2 = await _seed_entity_with_stage(
        pg_url, tenant_id=tenant_id, entity_type=entity_type, stage_progression="review"
    )

    with patch_validator_for_actor(admin_persona):
        resp = await admin_client.put(
            f"/v1/admin/tenants/{tenant_id}/progression-definitions/{progression_id}",
            json={
                "definition": _ENFORCING_DEFINITION,
                "is_advisory": False,
                "force": False,
            },
            headers=bearer_headers(tenant_slug=admin_persona.slug),
        )
    assert resp.status_code == 409, f"expected 409, got {resp.status_code}: {resp.text}"
    body = resp.json()
    error_item = body.get("errors", [{}])[0]
    assert error_item.get("code") == "preflight_offenders_present", f"unexpected body: {body}"
    import json as _json  # noqa: PLC0415

    message_payload = _json.loads(error_item.get("message", "{}"))
    offenders = message_payload.get("offenders", [])
    assert len(offenders) == 2, f"expected 2 offenders, got {offenders}"
    offender_ids = {o["entity_id"] for o in offenders}
    assert str(offender1) in offender_ids
    assert str(offender2) in offender_ids
    assert "hint" in message_payload, "hint must be present in 409 message payload"

    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await session.execute(
                select(ProgressionDefinition).where(
                    ProgressionDefinition.tenant_id == tenant_id,
                    ProgressionDefinition.entity_type == entity_type,
                    ProgressionDefinition.is_advisory.is_(False),
                )
            )
            enforcing_rows = result.scalars().all()
    finally:
        await engine.dispose()
    assert len(enforcing_rows) == 0, "no enforcing definition must be written when offenders exist"
