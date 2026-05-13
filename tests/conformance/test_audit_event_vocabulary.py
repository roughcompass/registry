"""Audit-event vocabulary conformance gate.

Asserts that every progression audit event written to the audit_log table
has the required keys in its after_jsonb payload. A missing key is a
conformance failure — callers consuming these events (dashboards, alerting,
downstream consumers) depend on a stable vocabulary.

Required keys per event type (as emitted by the service layer):
  progression.transition.accepted   — entity_id, from_state, to_state, definition_id
  progression.transition.rejected   — entity_id, from_state, to_state, definition_id, reason
  progression.transition.warned     — entity_id, from_state, to_state, definition_id, reason
  progression.transition.overridden — entity_id, override_id, from_state, to_state, gate_id, authorized_by
  progression.definition.published  — progression_id, entity_type, is_advisory
  progression.definition.soft_deleted — progression_id, entity_type
  progression.override.created      — override_id, entity_id, gate_id, t_valid_to

Each scenario runs the relevant HTTP path via the live FastAPI app (testcontainers
Postgres) and then queries audit_log to assert the JSONB shape.
"""

from __future__ import annotations

import datetime
import secrets
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.api.auth.tokens import hash_token
from registry.config import Settings
from registry.main import create_app

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Seed helpers (shared across scenarios)
# ---------------------------------------------------------------------------


async def _seed_tenant_admin(
    pg_url: str,
    *,
    slug: str,
) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Create tenant + admin actor + api_token. Returns (tenant_id, actor_id, raw_token)."""
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
                    "VALUES (:aid, :tid, 'vocab-actor', :now)"
                ),
                {"aid": actor_id, "tid": tenant_id, "now": _NOW},
            )
            await session.execute(
                text(
                    "INSERT INTO api_tokens "
                    "(token_id, tenant_id, actor_id, token_hash, roles, created_at) "
                    "VALUES (gen_random_uuid(), :tid, :aid, :th, :roles, :now)"
                ),
                {
                    "tid": tenant_id, "aid": actor_id,
                    "th": hash_token(raw_token),
                    "roles": ["admin", "producer"],
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
    stage_progression: str | None = None,
) -> uuid.UUID:
    entity_id = uuid.uuid4()
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
                {"eid": entity_id, "tid": tenant_id, "etype": entity_type,
                 "name": f"ent-{entity_id}", "now": _NOW},
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
                    {"tid": tenant_id, "eid": entity_id,
                     "val": f'"{stage_progression}"', "now": _NOW},
                )
    finally:
        await engine.dispose()
    return entity_id


async def _seed_attribute(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    entity_id: uuid.UUID,
    key: str,
    value: object,
) -> None:
    import json as _json  # noqa: PLC0415

    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO attributes "
                    "(attr_id, tenant_id, entity_id, key, value, "
                    " t_valid_from, t_valid_to, t_ingested_at, t_invalidated_at) "
                    "VALUES (gen_random_uuid(), :tid, :eid, :key, "
                    "        CAST(:val AS jsonb), :now, NULL, :now, NULL)"
                ),
                {"tid": tenant_id, "eid": entity_id,
                 "key": key, "val": _json.dumps(value), "now": _NOW},
            )
    finally:
        await engine.dispose()


async def _fetch_audit_payload(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    action: str,
    entity_id: uuid.UUID | None = None,
    progression_id: str | None = None,
    override_id: str | None = None,
) -> dict | None:
    """Query audit_log for the most recent row matching the action + optional payload filters.

    ``progression_id`` matches against both ``progression_id`` and ``definition_id``
    keys since different event types use different key names for the same value.
    """
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    clauses = ["tenant_id = :tid", "action = :action"]
    params: dict = {"tid": tenant_id, "action": action}

    if entity_id is not None:
        clauses.append("after_jsonb->>'entity_id' = :eid")
        params["eid"] = str(entity_id)
    if progression_id is not None:
        # The transition events use definition_id; definition events use progression_id.
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


def _make_app(pg_url: str, app_settings: Settings) -> object:
    return create_app(app_settings)


# ---------------------------------------------------------------------------
# progression.transition.accepted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_vocab_transition_accepted(pg_container: str, app_settings: Settings) -> None:
    """progression.transition.accepted must carry entity_id, from_state, to_state, definition_id."""
    slug = f"vocab-accept-{secrets.token_hex(4)}"
    entity_type = f"et-vacpt-{secrets.token_hex(4)}"
    tenant_id, _, token = await _seed_tenant_admin(pg_container, slug=slug)

    app = _make_app(pg_container, app_settings)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {token}"

        def_resp = await client.post(
            f"/v1/admin/tenants/{tenant_id}/progression-definitions",
            json={
                "entity_type": entity_type,
                "definition": {
                    "states": [{"id": "1", "name": "Draft"}, {"id": "2", "name": "Published"}],
                    "transitions": {"forward": "sequential"},
                },
                "is_advisory": False,
            },
        )
        assert def_resp.status_code == 201, def_resp.text
        definition_id = def_resp.json()["progression_id"]

        entity_id = await _seed_entity(
            pg_container, tenant_id=tenant_id, entity_type=entity_type,
            stage_progression="1",
        )

        patch_resp = await client.patch(
            f"/v1/capabilities/{entity_id}",
            json={"updates": {"stage_progression": "2"}},
        )
        assert patch_resp.status_code == 200, patch_resp.text

    payload = await _fetch_audit_payload(
        pg_container, tenant_id=tenant_id,
        action="progression.transition.accepted", entity_id=entity_id,
    )
    assert payload is not None, "progression.transition.accepted audit row must exist"
    for key in ("entity_id", "from_state", "to_state", "definition_id"):
        assert key in payload, f"required key '{key}' missing from accepted audit payload: {payload}"
    assert payload["definition_id"] == definition_id


# ---------------------------------------------------------------------------
# progression.transition.rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_vocab_transition_rejected(pg_container: str, app_settings: Settings) -> None:
    """progression.transition.rejected must carry entity_id, from_state, to_state, definition_id, reason."""
    slug = f"vocab-reject-{secrets.token_hex(4)}"
    entity_type = f"et-vrej-{secrets.token_hex(4)}"
    tenant_id, _, token = await _seed_tenant_admin(pg_container, slug=slug)

    app = _make_app(pg_container, app_settings)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {token}"

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
        )
        assert def_resp.status_code == 201, def_resp.text

        entity_id = await _seed_entity(
            pg_container, tenant_id=tenant_id, entity_type=entity_type,
            stage_progression="1",
        )

        # Gate unsatisfied — rejected.
        patch_resp = await client.patch(
            f"/v1/capabilities/{entity_id}",
            json={"updates": {"stage_progression": "2"}},
        )
        assert patch_resp.status_code == 422, patch_resp.text

    payload = await _fetch_audit_payload(
        pg_container, tenant_id=tenant_id,
        action="progression.transition.rejected", entity_id=entity_id,
    )
    assert payload is not None, "progression.transition.rejected audit row must exist"
    for key in ("entity_id", "from_state", "to_state", "definition_id", "reason"):
        assert key in payload, f"required key '{key}' missing from rejected audit payload: {payload}"


# ---------------------------------------------------------------------------
# progression.transition.warned
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_vocab_transition_warned(pg_container: str, app_settings: Settings) -> None:
    """progression.transition.warned must carry entity_id, from_state, to_state, definition_id, reason."""
    slug = f"vocab-warn-{secrets.token_hex(4)}"
    entity_type = f"et-vwrn-{secrets.token_hex(4)}"
    tenant_id, _, token = await _seed_tenant_admin(pg_container, slug=slug)

    app = _make_app(pg_container, app_settings)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {token}"

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
                "is_advisory": True,
            },
        )
        assert def_resp.status_code == 201, def_resp.text

        entity_id = await _seed_entity(
            pg_container, tenant_id=tenant_id, entity_type=entity_type,
            stage_progression="1",
        )

        # Gate unsatisfied but advisory — warned.
        patch_resp = await client.patch(
            f"/v1/capabilities/{entity_id}",
            json={"updates": {"stage_progression": "2"}},
        )
        assert patch_resp.status_code == 200, patch_resp.text

    payload = await _fetch_audit_payload(
        pg_container, tenant_id=tenant_id,
        action="progression.transition.warned", entity_id=entity_id,
    )
    assert payload is not None, "progression.transition.warned audit row must exist"
    for key in ("entity_id", "from_state", "to_state", "definition_id", "reason"):
        assert key in payload, f"required key '{key}' missing from warned audit payload: {payload}"


# ---------------------------------------------------------------------------
# progression.transition.overridden
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_vocab_transition_overridden(pg_container: str, app_settings: Settings) -> None:
    """progression.transition.overridden must carry required audit keys.

    Required keys: entity_id, override_id, from_state, to_state, gate_id, authorized_by.
    Verifies the required keys are present in after_jsonb for this audit event.
    """
    slug = f"vocab-over-{secrets.token_hex(4)}"
    entity_type = f"et-vovr-{secrets.token_hex(4)}"
    tenant_id, _, token = await _seed_tenant_admin(pg_container, slug=slug)

    app = _make_app(pg_container, app_settings)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {token}"

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
        )
        assert def_resp.status_code == 201, def_resp.text

        entity_id = await _seed_entity(
            pg_container, tenant_id=tenant_id, entity_type=entity_type,
            stage_progression="1",
        )

        # Create override.
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
        )
        assert ovr_resp.status_code == 201, ovr_resp.text
        override_id = ovr_resp.json()["override_id"]

        # Gate still unsatisfied — override applies.
        patch_resp = await client.patch(
            f"/v1/capabilities/{entity_id}",
            json={"updates": {"stage_progression": "2"}},
        )
        assert patch_resp.status_code == 200, patch_resp.text

    payload = await _fetch_audit_payload(
        pg_container, tenant_id=tenant_id,
        action="progression.transition.overridden", entity_id=entity_id,
        override_id=override_id,
    )
    assert payload is not None, "progression.transition.overridden audit row must exist"
    for key in ("entity_id", "override_id", "from_state", "to_state", "gate_id", "authorized_by"):
        assert key in payload, f"required key '{key}' missing from overridden audit payload: {payload}"


# ---------------------------------------------------------------------------
# progression.definition.published
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_vocab_definition_published(pg_container: str, app_settings: Settings) -> None:
    """progression.definition.published must carry definition_id, tenant_id, entity_type, advisory."""
    slug = f"vocab-pub-{secrets.token_hex(4)}"
    entity_type = f"et-vpub-{secrets.token_hex(4)}"
    tenant_id, _, token = await _seed_tenant_admin(pg_container, slug=slug)

    app = _make_app(pg_container, app_settings)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {token}"

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
        )
        assert def_resp.status_code == 201, def_resp.text
        definition_id = def_resp.json()["progression_id"]

    payload = await _fetch_audit_payload(
        pg_container, tenant_id=tenant_id,
        action="progression.definition.published",
        progression_id=definition_id,
    )
    assert payload is not None, "progression.definition.published audit row must exist"
    for key in ("progression_id", "entity_type", "is_advisory"):
        assert key in payload, f"required key '{key}' missing from definition.published payload: {payload}"
    assert payload["progression_id"] == definition_id
    assert payload["entity_type"] == entity_type


# ---------------------------------------------------------------------------
# progression.definition.soft_deleted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_vocab_definition_soft_deleted(pg_container: str, app_settings: Settings) -> None:
    """progression.definition.soft_deleted must carry definition_id, tenant_id, entity_type."""
    slug = f"vocab-del-{secrets.token_hex(4)}"
    entity_type = f"et-vdel-{secrets.token_hex(4)}"
    tenant_id, _, token = await _seed_tenant_admin(pg_container, slug=slug)

    app = _make_app(pg_container, app_settings)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {token}"

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
        )
        assert def_resp.status_code == 201, def_resp.text
        definition_id = def_resp.json()["progression_id"]

        del_resp = await client.delete(
            f"/v1/admin/tenants/{tenant_id}/progression-definitions/{definition_id}"
        )
        assert del_resp.status_code == 204, del_resp.text

    payload = await _fetch_audit_payload(
        pg_container, tenant_id=tenant_id,
        action="progression.definition.soft_deleted",
        progression_id=definition_id,
    )
    assert payload is not None, "progression.definition.soft_deleted audit row must exist"
    for key in ("progression_id", "entity_type"):
        assert key in payload, f"required key '{key}' missing from definition.soft_deleted payload: {payload}"
    assert payload["progression_id"] == definition_id


# ---------------------------------------------------------------------------
# progression.override.created
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_vocab_override_created(pg_container: str, app_settings: Settings) -> None:
    """progression.override.created must carry override_id, entity_id, gate_id, t_valid_to."""
    slug = f"vocab-ovcr-{secrets.token_hex(4)}"
    tenant_id, _, token = await _seed_tenant_admin(pg_container, slug=slug)

    app = _make_app(pg_container, app_settings)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {token}"

        # Seed entity.
        entity_id_raw = uuid.uuid4()
        engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session, session.begin():
                await session.execute(
                    text(
                        "INSERT INTO entities "
                        "(entity_id, tenant_id, entity_type, name, is_active, created_at) "
                        "VALUES (:eid, :tid, 'capability', :name, TRUE, :now)"
                    ),
                    {"eid": entity_id_raw, "tid": tenant_id,
                     "name": f"ent-ovcr-{entity_id_raw}", "now": _NOW},
                )
        finally:
            await engine.dispose()

        ovr_resp = await client.post(
            f"/v1/admin/tenants/{tenant_id}/entities/{entity_id_raw}/progression-overrides",
            json={
                "from_state": "1",
                "to_state": "2",
                "gate_id": "review_approved",
                "bypass_skip_rules": False,
                "reason": "vocab test override",
                "t_valid_to": "2099-12-31T23:59:59Z",
            },
        )
        assert ovr_resp.status_code == 201, ovr_resp.text
        override_id = ovr_resp.json()["override_id"]

    payload = await _fetch_audit_payload(
        pg_container, tenant_id=tenant_id,
        action="progression.override.created",
        override_id=override_id,
    )
    assert payload is not None, "progression.override.created audit row must exist"
    for key in ("override_id", "entity_id", "gate_id", "t_valid_to"):
        assert key in payload, f"required key '{key}' missing from override.created payload: {payload}"
    assert payload["override_id"] == override_id
