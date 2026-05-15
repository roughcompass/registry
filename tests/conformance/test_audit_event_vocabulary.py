"""Audit-event vocabulary conformance gate.

Asserts that every progression audit event written to ``audit_log`` has
the required keys in its ``after_jsonb`` payload. A missing key is a
conformance failure — dashboards, alerting, and downstream consumers
depend on a stable vocabulary.

Required keys per event type
----------------------------
  progression.transition.accepted    — entity_id, from_state, to_state, definition_id
  progression.transition.rejected    — entity_id, from_state, to_state, definition_id, reason
  progression.transition.warned      — entity_id, from_state, to_state, definition_id, reason
  progression.transition.overridden  — entity_id, override_id, from_state, to_state, gate_id, authorized_by
  progression.definition.published   — progression_id, entity_type, is_advisory
  progression.definition.soft_deleted — progression_id, entity_type
  progression.override.created       — override_id, entity_id, gate_id, t_valid_to

Each scenario drives the relevant HTTP path via the live FastAPI app
(testcontainers Postgres) using the entitlement-resolved auth harness
in tests/helpers/auth_harness.py, then queries audit_log to assert the
JSONB shape.
"""

from __future__ import annotations

import datetime
import json
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

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def harness(pg_container: str) -> AsyncIterator[EntitlementAuthHarness]:
    async with EntitlementAuthHarness(pg_container) as h:
        yield h


async def _materialise(
    h: EntitlementAuthHarness, client: AsyncClient, persona: TenantPersona
) -> uuid.UUID:
    """JIT the tenant + actor and return the tenant_id."""
    h.configure_fetcher_for(persona)
    with patch_validator_for_actor(persona):
        resp = await client.get(
            "/v1/whoami", headers=bearer_headers(tenant_slug=persona.slug)
        )
        assert resp.status_code == 200, resp.text
    return await _lookup_tenant_id(h._pg_url, persona.slug)


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


async def _seed_entity(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    entity_type: str,
    stage_progression: str | None = None,
) -> uuid.UUID:
    """Insert one entity row + optional stage_progression attribute."""
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
                    "VALUES (:eid, :tid, :etype, :name, TRUE, :now)"
                ),
                {
                    "eid": entity_id,
                    "tid": tenant_id,
                    "etype": entity_type,
                    "name": f"ent-{entity_id}",
                    "now": _NOW,
                },
            )
            if stage_progression is not None:
                await session.execute(
                    text(
                        "INSERT INTO attributes "
                        "(attr_id, tenant_id, entity_id, key, value, "
                        " t_valid_from, t_valid_to, t_ingested_at, t_invalidated_at) "
                        "VALUES (gen_random_uuid(), :tid, :eid, 'stage_progression', "
                        "        CAST(:val AS jsonb), :now, NULL, :now, NULL)"
                    ),
                    {
                        "tid": tenant_id,
                        "eid": entity_id,
                        "val": json.dumps(stage_progression),
                        "now": _NOW,
                    },
                )
    finally:
        await engine.dispose()
    return entity_id


async def _fetch_audit_payload(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    action: str,
    entity_id: uuid.UUID | None = None,
    progression_id: str | None = None,
    override_id: str | None = None,
) -> dict[str, object] | None:
    """Return the most recent audit_log row matching the action +
    optional payload filters; ``None`` if no row matches."""
    engine = create_async_engine(
        pg_url, connect_args={"prepared_statement_cache_size": 0}
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    clauses = ["tenant_id = :tid", "action = :action"]
    params: dict[str, object] = {"tid": tenant_id, "action": action}
    if entity_id is not None:
        clauses.append("after_jsonb->>'entity_id' = :eid")
        params["eid"] = str(entity_id)
    if progression_id is not None:
        # Transition events use definition_id; definition events use progression_id.
        clauses.append(
            "(after_jsonb->>'definition_id' = :pid OR after_jsonb->>'progression_id' = :pid)"
        )
        params["pid"] = progression_id
    if override_id is not None:
        clauses.append("after_jsonb->>'override_id' = :oid")
        params["oid"] = override_id
    where = " AND ".join(clauses)
    try:
        async with factory() as session:
            result = await session.execute(
                text(f"SELECT after_jsonb FROM audit_log WHERE {where} ORDER BY ts DESC LIMIT 1"),
                params,
            )
            row = result.fetchone()
    finally:
        await engine.dispose()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# progression.transition.accepted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_vocab_transition_accepted(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    suffix = uuid.uuid4().hex[:6]
    entity_type = f"et-vacpt-{suffix}"
    persona = harness.add_persona(
        f"vocab-accept-{suffix}", roles=["admin", "producer"]
    )
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        tenant_id = await _materialise(harness, client, persona)
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            def_resp = await client.post(
                f"/v1/admin/tenants/{tenant_id}/progression-definitions",
                json={
                    "entity_type": entity_type,
                    "definition": {
                        "states": [
                            {"id": "1", "name": "Draft"},
                            {"id": "2", "name": "Published"},
                        ],
                        "transitions": {"forward": "sequential"},
                    },
                    "is_advisory": False,
                },
                headers=bearer_headers(tenant_slug=persona.slug),
            )
            assert def_resp.status_code == 201, def_resp.text
            definition_id = def_resp.json()["progression_id"]

            entity_id = await _seed_entity(
                pg_container,
                tenant_id=tenant_id,
                entity_type=entity_type,
                stage_progression="1",
            )

            patch_resp = await client.patch(
                f"/v1/capabilities/{entity_id}",
                json={"updates": {"stage_progression": "2"}},
                headers=bearer_headers(tenant_slug=persona.slug),
            )
            assert patch_resp.status_code == 200, patch_resp.text

    payload = await _fetch_audit_payload(
        pg_container,
        tenant_id=tenant_id,
        action="progression.transition.accepted",
        entity_id=entity_id,
    )
    assert payload is not None, "progression.transition.accepted audit row must exist"
    for key in ("entity_id", "from_state", "to_state", "definition_id"):
        assert key in payload, f"required key '{key}' missing: {payload}"
    assert payload["definition_id"] == definition_id


# ---------------------------------------------------------------------------
# progression.transition.rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_vocab_transition_rejected(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    suffix = uuid.uuid4().hex[:6]
    entity_type = f"et-vrej-{suffix}"
    persona = harness.add_persona(
        f"vocab-reject-{suffix}", roles=["admin", "producer"]
    )
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        tenant_id = await _materialise(harness, client, persona)
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            def_resp = await client.post(
                f"/v1/admin/tenants/{tenant_id}/progression-definitions",
                json={
                    "entity_type": entity_type,
                    "definition": {
                        "states": [
                            {"id": "1", "name": "Draft"},
                            {"id": "2", "name": "Published", "gates": ["approved"]},
                        ],
                        "transitions": {"forward": "sequential"},
                    },
                    "is_advisory": False,
                },
                headers=bearer_headers(tenant_slug=persona.slug),
            )
            assert def_resp.status_code == 201, def_resp.text
            entity_id = await _seed_entity(
                pg_container,
                tenant_id=tenant_id,
                entity_type=entity_type,
                stage_progression="1",
            )
            patch_resp = await client.patch(
                f"/v1/capabilities/{entity_id}",
                json={"updates": {"stage_progression": "2"}},
                headers=bearer_headers(tenant_slug=persona.slug),
            )
            # Gate unsatisfied → rejected (422).
            assert patch_resp.status_code == 422, patch_resp.text

    payload = await _fetch_audit_payload(
        pg_container,
        tenant_id=tenant_id,
        action="progression.transition.rejected",
        entity_id=entity_id,
    )
    assert payload is not None, "progression.transition.rejected audit row must exist"
    for key in ("entity_id", "from_state", "to_state", "definition_id", "reason"):
        assert key in payload, f"required key '{key}' missing: {payload}"


# ---------------------------------------------------------------------------
# progression.transition.warned
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_vocab_transition_warned(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    suffix = uuid.uuid4().hex[:6]
    entity_type = f"et-vwrn-{suffix}"
    persona = harness.add_persona(
        f"vocab-warn-{suffix}", roles=["admin", "producer"]
    )
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        tenant_id = await _materialise(harness, client, persona)
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            def_resp = await client.post(
                f"/v1/admin/tenants/{tenant_id}/progression-definitions",
                json={
                    "entity_type": entity_type,
                    "definition": {
                        "states": [
                            {"id": "1", "name": "Draft"},
                            {"id": "2", "name": "Published", "gates": ["approved"]},
                        ],
                        "transitions": {"forward": "sequential"},
                    },
                    "is_advisory": True,  # advisory ⇒ gate failure becomes warning, not rejection
                },
                headers=bearer_headers(tenant_slug=persona.slug),
            )
            assert def_resp.status_code == 201, def_resp.text
            entity_id = await _seed_entity(
                pg_container,
                tenant_id=tenant_id,
                entity_type=entity_type,
                stage_progression="1",
            )
            patch_resp = await client.patch(
                f"/v1/capabilities/{entity_id}",
                json={"updates": {"stage_progression": "2"}},
                headers=bearer_headers(tenant_slug=persona.slug),
            )
            assert patch_resp.status_code == 200, patch_resp.text

    payload = await _fetch_audit_payload(
        pg_container,
        tenant_id=tenant_id,
        action="progression.transition.warned",
        entity_id=entity_id,
    )
    assert payload is not None, "progression.transition.warned audit row must exist"
    for key in ("entity_id", "from_state", "to_state", "definition_id", "reason"):
        assert key in payload, f"required key '{key}' missing: {payload}"


# ---------------------------------------------------------------------------
# progression.transition.overridden
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_vocab_transition_overridden(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    suffix = uuid.uuid4().hex[:6]
    entity_type = f"et-vovr-{suffix}"
    persona = harness.add_persona(
        f"vocab-over-{suffix}", roles=["admin", "producer"]
    )
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        tenant_id = await _materialise(harness, client, persona)
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            def_resp = await client.post(
                f"/v1/admin/tenants/{tenant_id}/progression-definitions",
                json={
                    "entity_type": entity_type,
                    "definition": {
                        "states": [
                            {"id": "1", "name": "Draft"},
                            {"id": "2", "name": "Published", "gates": ["approved"]},
                        ],
                        "transitions": {"forward": "sequential"},
                    },
                    "is_advisory": False,
                },
                headers=bearer_headers(tenant_slug=persona.slug),
            )
            assert def_resp.status_code == 201, def_resp.text
            entity_id = await _seed_entity(
                pg_container,
                tenant_id=tenant_id,
                entity_type=entity_type,
                stage_progression="1",
            )

            ovr_resp = await client.post(
                f"/v1/admin/tenants/{tenant_id}/entities/{entity_id}/progression-overrides",
                json={
                    "from_state": "1",
                    "to_state": "2",
                    "gate_id": "approved",
                    "bypass_skip_rules": False,
                    "reason": "vocab-test exception",
                    "t_valid_to": "2099-12-31T23:59:59Z",
                },
                headers=bearer_headers(tenant_slug=persona.slug),
            )
            assert ovr_resp.status_code == 201, ovr_resp.text
            override_id = ovr_resp.json()["override_id"]

            patch_resp = await client.patch(
                f"/v1/capabilities/{entity_id}",
                json={"updates": {"stage_progression": "2"}},
                headers=bearer_headers(tenant_slug=persona.slug),
            )
            assert patch_resp.status_code == 200, patch_resp.text

    payload = await _fetch_audit_payload(
        pg_container,
        tenant_id=tenant_id,
        action="progression.transition.overridden",
        entity_id=entity_id,
        override_id=override_id,
    )
    assert payload is not None, "progression.transition.overridden audit row must exist"
    for key in ("entity_id", "override_id", "from_state", "to_state", "gate_id", "authorized_by"):
        assert key in payload, f"required key '{key}' missing: {payload}"


# ---------------------------------------------------------------------------
# progression.definition.published
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_vocab_definition_published(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    suffix = uuid.uuid4().hex[:6]
    entity_type = f"et-vpub-{suffix}"
    persona = harness.add_persona(
        f"vocab-pub-{suffix}", roles=["admin", "producer"]
    )
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        tenant_id = await _materialise(harness, client, persona)
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            def_resp = await client.post(
                f"/v1/admin/tenants/{tenant_id}/progression-definitions",
                json={
                    "entity_type": entity_type,
                    "definition": {
                        "states": [{"id": "draft", "name": "Draft"}],
                        "transitions": {"forward": "sequential"},
                    },
                    "is_advisory": True,
                },
                headers=bearer_headers(tenant_slug=persona.slug),
            )
            assert def_resp.status_code == 201, def_resp.text
            definition_id = def_resp.json()["progression_id"]

    payload = await _fetch_audit_payload(
        pg_container,
        tenant_id=tenant_id,
        action="progression.definition.published",
        progression_id=definition_id,
    )
    assert payload is not None, "progression.definition.published audit row must exist"
    for key in ("progression_id", "entity_type", "is_advisory"):
        assert key in payload, f"required key '{key}' missing: {payload}"
    assert payload["progression_id"] == definition_id
    assert payload["entity_type"] == entity_type


# ---------------------------------------------------------------------------
# progression.definition.soft_deleted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_vocab_definition_soft_deleted(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    suffix = uuid.uuid4().hex[:6]
    entity_type = f"et-vdel-{suffix}"
    persona = harness.add_persona(
        f"vocab-del-{suffix}", roles=["admin", "producer"]
    )
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        tenant_id = await _materialise(harness, client, persona)
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            def_resp = await client.post(
                f"/v1/admin/tenants/{tenant_id}/progression-definitions",
                json={
                    "entity_type": entity_type,
                    "definition": {
                        "states": [{"id": "draft", "name": "Draft"}],
                        "transitions": {"forward": "sequential"},
                    },
                    "is_advisory": True,
                },
                headers=bearer_headers(tenant_slug=persona.slug),
            )
            assert def_resp.status_code == 201, def_resp.text
            definition_id = def_resp.json()["progression_id"]

            del_resp = await client.delete(
                f"/v1/admin/tenants/{tenant_id}/progression-definitions/{definition_id}",
                headers=bearer_headers(tenant_slug=persona.slug),
            )
            assert del_resp.status_code == 204, del_resp.text

    payload = await _fetch_audit_payload(
        pg_container,
        tenant_id=tenant_id,
        action="progression.definition.soft_deleted",
        progression_id=definition_id,
    )
    assert payload is not None, "progression.definition.soft_deleted audit row must exist"
    for key in ("progression_id", "entity_type"):
        assert key in payload, f"required key '{key}' missing: {payload}"
    assert payload["progression_id"] == definition_id


# ---------------------------------------------------------------------------
# progression.override.created
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_vocab_override_created(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    suffix = uuid.uuid4().hex[:6]
    persona = harness.add_persona(
        f"vocab-ovcr-{suffix}", roles=["admin", "producer"]
    )
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        tenant_id = await _materialise(harness, client, persona)

        # Seed a bare entity (no progression definition needed for this event).
        entity_id = await _seed_entity(
            pg_container, tenant_id=tenant_id, entity_type="capability"
        )

        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            ovr_resp = await client.post(
                f"/v1/admin/tenants/{tenant_id}/entities/{entity_id}/progression-overrides",
                json={
                    "from_state": "1",
                    "to_state": "2",
                    "gate_id": "review_approved",
                    "bypass_skip_rules": False,
                    "reason": "vocab test override",
                    "t_valid_to": "2099-12-31T23:59:59Z",
                },
                headers=bearer_headers(tenant_slug=persona.slug),
            )
            assert ovr_resp.status_code == 201, ovr_resp.text
            override_id = ovr_resp.json()["override_id"]

    payload = await _fetch_audit_payload(
        pg_container,
        tenant_id=tenant_id,
        action="progression.override.created",
        override_id=override_id,
    )
    assert payload is not None, "progression.override.created audit row must exist"
    for key in ("override_id", "entity_id", "gate_id", "t_valid_to"):
        assert key in payload, f"required key '{key}' missing: {payload}"
    assert payload["override_id"] == override_id
