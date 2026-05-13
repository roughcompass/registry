"""Integration tests for the full progression write path.

Exercises the sequence: PATCH /v1/capabilities/{id} with stage_progression
→ EntityService.update_entity → ProgressionService.validate_transition
→ audit event written → attribute written (or rejected).

Scenarios:
1. Accepted transition — all gates satisfied, audit event accepted, write succeeds.
2. Rejected transition (enforcing) — gate unsatisfied, HTTP 422, audit event rejected,
   attribute unchanged in DB.
3. Warned transition (advisory) — gate unsatisfied but advisory, write succeeds,
   audit event warned, warnings in response body.
4. Overridden transition — gate fails, matching unconsumed override exists, write
   succeeds, override consumed, audit event overridden.
5. Tenant isolation — progression definition in tenant A does not affect tenant B's
   entity write for the same entity_type (pass-through when B has no definition).
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
# Minimal valid progression definition with one gate on the destination state.
# State "1" → "2" is the only forward transition.  State "2" requires gate
# "review_approved" to be satisfied (truthy entity attribute).
# ---------------------------------------------------------------------------

_DEFINITION_WITH_GATE = {
    "states": [
        {"id": "1", "name": "Draft"},
        {"id": "2", "name": "Review", "gates": ["review_approved"]},
    ],
    "transitions": {"forward": "sequential"},
}

_DEFINITION_NO_GATE = {
    "states": [
        {"id": "1", "name": "Draft"},
        {"id": "2", "name": "Published"},
    ],
    "transitions": {"forward": "sequential"},
}


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_tenant(
    pg_url: str,
    *,
    slug: str,
    roles: list[str],
) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Create tenant + actor + api_token. Returns (tenant_id, actor_id, raw_token)."""
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


async def _seed_entity(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    entity_type: str,
    name: str,
    stage_progression: str | None = None,
) -> uuid.UUID:
    """Insert an entity with optional stage_progression attribute. Returns entity_id."""
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
                 "name": name, "now": _NOW},
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
                    {"aid": attr_id, "tid": tenant_id, "eid": entity_id,
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
    """Insert an additional attribute row (for gate satisfaction)."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    import json as _json  # noqa: PLC0415

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


async def _get_stage_progression(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    entity_id: uuid.UUID,
) -> str | None:
    """Return the current (open) stage_progression attribute value, or None."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await session.execute(
                text(
                    "SELECT value FROM attributes "
                    "WHERE tenant_id = :tid AND entity_id = :eid "
                    "  AND key = 'stage_progression' AND t_valid_to IS NULL "
                    "  AND t_invalidated_at IS NULL "
                    "LIMIT 1"
                ),
                {"tid": tenant_id, "eid": entity_id},
            )
            row = result.fetchone()
    finally:
        await engine.dispose()
    if row is None:
        return None
    val = row[0]
    if isinstance(val, str):
        return val.strip('"')
    return str(val).strip('"')


async def _fetch_audit_row(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    action: str,
    entity_id: uuid.UUID,
) -> dict | None:
    """Return the most recent audit_log row for (tenant_id, action, entity_id)."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await session.execute(
                text(
                    "SELECT action, after_jsonb FROM audit_log "
                    "WHERE tenant_id = :tid AND action = :action "
                    "  AND after_jsonb->>'entity_id' = :eid "
                    "ORDER BY ts DESC LIMIT 1"
                ),
                {"tid": tenant_id, "action": action, "eid": str(entity_id)},
            )
            row = result.fetchone()
    finally:
        await engine.dispose()
    if row is None:
        return None
    return {"action": row[0], "after_jsonb": row[1]}


# ---------------------------------------------------------------------------
# Shared fixture factory
# ---------------------------------------------------------------------------


def _make_app(pg_url: str) -> object:
    settings = Settings(
        database_url=pg_url,
        pgbouncer_url=pg_url,
        scheduler_jobstore_url=pg_url,
        scheduler_use_memory_jobstore=True,
        embedding_model="stub",
    )
    return create_app(settings)


# ---------------------------------------------------------------------------
# Scenario 1: Accepted transition — all gates satisfied
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accepted_transition_writes_attribute_and_emits_audit(pg_container: str) -> None:
    """PATCH with satisfied gate: write succeeds and audit event accepted is emitted."""
    slug = f"prog-wp-accept-{secrets.token_hex(4)}"
    entity_type = f"et-accept-{secrets.token_hex(4)}"
    tenant_id, _, token = await _seed_tenant(pg_container, slug=slug, roles=["admin", "producer"])

    app = _make_app(pg_container)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {token}"

        # Create a progression definition with a gate on state "2".
        def_resp = await client.post(
            f"/v1/admin/tenants/{tenant_id}/progression-definitions",
            json={
                "entity_type": entity_type,
                "definition": _DEFINITION_WITH_GATE,
                "is_advisory": False,
            },
        )
        assert def_resp.status_code == 201, def_resp.text
        def_id = def_resp.json()["progression_id"]

        # Seed entity in state "1".
        entity_id = await _seed_entity(
            pg_container, tenant_id=tenant_id, entity_type=entity_type,
            name=f"ent-accept-{secrets.token_hex(4)}", stage_progression="1",
        )

        # Satisfy the gate attribute so the transition is accepted.
        await _seed_attribute(
            pg_container, tenant_id=tenant_id, entity_id=entity_id,
            key="review_approved", value=True,
        )

        # PATCH: move to state "2".
        patch_resp = await client.patch(
            f"/v1/capabilities/{entity_id}",
            json={"updates": {"stage_progression": "2"}},
        )
        assert patch_resp.status_code == 200, f"expected 200, got {patch_resp.status_code}: {patch_resp.text}"

    # Assert the attribute changed in DB.
    current_state = await _get_stage_progression(pg_container, tenant_id=tenant_id, entity_id=entity_id)
    assert current_state == "2", f"expected stage_progression='2', got {current_state!r}"

    # Assert audit event accepted was emitted.
    audit = await _fetch_audit_row(
        pg_container, tenant_id=tenant_id,
        action="progression.transition.accepted", entity_id=entity_id,
    )
    assert audit is not None, "progression.transition.accepted audit row must exist"
    payload = audit["after_jsonb"]
    assert payload["entity_id"] == str(entity_id)
    assert payload["from_state"] == "1"
    assert payload["to_state"] == "2"
    assert payload["definition_id"] == def_id


# ---------------------------------------------------------------------------
# Scenario 2: Rejected transition (enforcing) — gate unsatisfied
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejected_transition_returns_422_and_does_not_write(pg_container: str) -> None:
    """Enforcing-mode gate failure: HTTP 422, stage_progression unchanged, audit rejected."""
    slug = f"prog-wp-reject-{secrets.token_hex(4)}"
    entity_type = f"et-reject-{secrets.token_hex(4)}"
    tenant_id, _, token = await _seed_tenant(pg_container, slug=slug, roles=["admin", "producer"])

    app = _make_app(pg_container)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {token}"

        # Enforcing definition with gate on state "2".
        def_resp = await client.post(
            f"/v1/admin/tenants/{tenant_id}/progression-definitions",
            json={
                "entity_type": entity_type,
                "definition": _DEFINITION_WITH_GATE,
                "is_advisory": False,
            },
        )
        assert def_resp.status_code == 201, def_resp.text

        # Seed entity in state "1" — gate NOT satisfied.
        entity_id = await _seed_entity(
            pg_container, tenant_id=tenant_id, entity_type=entity_type,
            name=f"ent-reject-{secrets.token_hex(4)}", stage_progression="1",
        )

        patch_resp = await client.patch(
            f"/v1/capabilities/{entity_id}",
            json={"updates": {"stage_progression": "2"}},
        )
        assert patch_resp.status_code == 422, (
            f"expected 422 for rejected enforcing transition, got {patch_resp.status_code}: {patch_resp.text}"
        )
        body = patch_resp.json()
        # Response carries progression_rejected code inside the errors envelope.
        errors = body.get("errors", [])
        assert any("progression_rejected" in str(e) for e in errors), (
            f"expected progression_rejected code in errors: {body}"
        )

    # The stage_progression attribute must not have changed.
    current_state = await _get_stage_progression(pg_container, tenant_id=tenant_id, entity_id=entity_id)
    assert current_state == "1", f"attribute must remain '1' after rejection; got {current_state!r}"

    # Rejected audit event must exist.
    audit = await _fetch_audit_row(
        pg_container, tenant_id=tenant_id,
        action="progression.transition.rejected", entity_id=entity_id,
    )
    assert audit is not None, "progression.transition.rejected audit row must exist"
    payload = audit["after_jsonb"]
    assert payload["from_state"] == "1"
    assert payload["to_state"] == "2"
    assert "reason" in payload


# ---------------------------------------------------------------------------
# Scenario 3: Warned transition (advisory mode)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warned_transition_succeeds_and_emits_warned_audit(pg_container: str) -> None:
    """Advisory-mode gate failure: write succeeds, warnings in body, audit warned emitted."""
    slug = f"prog-wp-warn-{secrets.token_hex(4)}"
    entity_type = f"et-warn-{secrets.token_hex(4)}"
    tenant_id, _, token = await _seed_tenant(pg_container, slug=slug, roles=["admin", "producer"])

    app = _make_app(pg_container)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {token}"

        # Advisory definition — gate failure is non-blocking.
        def_resp = await client.post(
            f"/v1/admin/tenants/{tenant_id}/progression-definitions",
            json={
                "entity_type": entity_type,
                "definition": _DEFINITION_WITH_GATE,
                "is_advisory": True,
            },
        )
        assert def_resp.status_code == 201, def_resp.text

        # Seed entity in state "1" — gate NOT satisfied.
        entity_id = await _seed_entity(
            pg_container, tenant_id=tenant_id, entity_type=entity_type,
            name=f"ent-warn-{secrets.token_hex(4)}", stage_progression="1",
        )

        patch_resp = await client.patch(
            f"/v1/capabilities/{entity_id}",
            json={"updates": {"stage_progression": "2"}},
        )
        assert patch_resp.status_code == 200, (
            f"advisory mode must allow the write; got {patch_resp.status_code}: {patch_resp.text}"
        )

    # Stage changed in DB.
    current_state = await _get_stage_progression(pg_container, tenant_id=tenant_id, entity_id=entity_id)
    assert current_state == "2", f"stage must be updated to '2'; got {current_state!r}"

    # Warned audit event exists.
    audit = await _fetch_audit_row(
        pg_container, tenant_id=tenant_id,
        action="progression.transition.warned", entity_id=entity_id,
    )
    assert audit is not None, "progression.transition.warned audit row must exist"
    payload = audit["after_jsonb"]
    assert payload["from_state"] == "1"
    assert payload["to_state"] == "2"
    assert "reason" in payload


# ---------------------------------------------------------------------------
# Scenario 4: Overridden transition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_overridden_transition_consumes_override_and_emits_audit(pg_container: str) -> None:
    """Gate fails, but matching unconsumed override exists: write succeeds, override consumed."""
    slug = f"prog-wp-over-{secrets.token_hex(4)}"
    entity_type = f"et-over-{secrets.token_hex(4)}"
    tenant_id, _, token = await _seed_tenant(pg_container, slug=slug, roles=["admin", "producer"])

    app = _make_app(pg_container)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {token}"

        # Enforcing definition — gate is unsatisfied.
        def_resp = await client.post(
            f"/v1/admin/tenants/{tenant_id}/progression-definitions",
            json={
                "entity_type": entity_type,
                "definition": _DEFINITION_WITH_GATE,
                "is_advisory": False,
            },
        )
        assert def_resp.status_code == 201, def_resp.text

        # Seed entity in state "1".
        entity_id = await _seed_entity(
            pg_container, tenant_id=tenant_id, entity_type=entity_type,
            name=f"ent-over-{secrets.token_hex(4)}", stage_progression="1",
        )

        # Create a matching override for this entity (from_state=1, to_state=2, gate_id=review_approved).
        override_resp = await client.post(
            f"/v1/admin/tenants/{tenant_id}/entities/{entity_id}/progression-overrides",
            json={
                "from_state": "1",
                "to_state": "2",
                "gate_id": "review_approved",
                "bypass_skip_rules": False,
                "reason": "CTO override for demo entity",
                "t_valid_to": "2099-12-31T23:59:59Z",
            },
        )
        assert override_resp.status_code == 201, override_resp.text
        override_id = override_resp.json()["override_id"]

        # PATCH — gate still unsatisfied, but override should apply.
        patch_resp = await client.patch(
            f"/v1/capabilities/{entity_id}",
            json={"updates": {"stage_progression": "2"}},
        )
        assert patch_resp.status_code == 200, (
            f"override must allow the write; got {patch_resp.status_code}: {patch_resp.text}"
        )

    # Stage changed in DB.
    current_state = await _get_stage_progression(pg_container, tenant_id=tenant_id, entity_id=entity_id)
    assert current_state == "2", f"stage must be updated to '2' with override; got {current_state!r}"

    # Override must be consumed (consumed_at set).
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await session.execute(
                text(
                    "SELECT consumed_at FROM progression_overrides "
                    "WHERE override_id = :oid"
                ),
                {"oid": uuid.UUID(override_id)},
            )
            row = result.fetchone()
    finally:
        await engine.dispose()
    assert row is not None, "override row must still exist"
    assert row[0] is not None, "consumed_at must be set after override is used"

    # Overridden audit event exists.
    audit = await _fetch_audit_row(
        pg_container, tenant_id=tenant_id,
        action="progression.transition.overridden", entity_id=entity_id,
    )
    assert audit is not None, "progression.transition.overridden audit row must exist"
    payload = audit["after_jsonb"]
    assert payload["entity_id"] == str(entity_id)
    assert payload["override_id"] == override_id
    assert payload["from_state"] == "1"
    assert payload["to_state"] == "2"
    assert "gate_id" in payload
    assert "authorized_by" in payload


# ---------------------------------------------------------------------------
# Scenario 5: Tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_isolation_definition_does_not_affect_other_tenant(pg_container: str) -> None:
    """Tenant A's progression definition must not block tenant B's entity write for the same entity_type."""
    entity_type = f"et-iso-{secrets.token_hex(4)}"

    slug_a = f"prog-wp-iso-a-{secrets.token_hex(4)}"
    tenant_a_id, _, token_a = await _seed_tenant(pg_container, slug=slug_a, roles=["admin", "producer"])

    slug_b = f"prog-wp-iso-b-{secrets.token_hex(4)}"
    tenant_b_id, _, token_b = await _seed_tenant(pg_container, slug=slug_b, roles=["admin", "producer"])

    app = _make_app(pg_container)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Create enforcing definition (gate required) for tenant A only.
        client.headers["Authorization"] = f"Bearer {token_a}"
        def_resp = await client.post(
            f"/v1/admin/tenants/{tenant_a_id}/progression-definitions",
            json={
                "entity_type": entity_type,
                "definition": _DEFINITION_WITH_GATE,
                "is_advisory": False,
            },
        )
        assert def_resp.status_code == 201, def_resp.text

        # Seed entity in state "1" under tenant B — no definition for B.
        entity_b_id = await _seed_entity(
            pg_container, tenant_id=tenant_b_id, entity_type=entity_type,
            name=f"ent-iso-b-{secrets.token_hex(4)}", stage_progression="1",
        )

        # Tenant B's write must pass through (no definition → no gate).
        client.headers["Authorization"] = f"Bearer {token_b}"
        patch_resp = await client.patch(
            f"/v1/capabilities/{entity_b_id}",
            json={"updates": {"stage_progression": "2"}},
        )
        assert patch_resp.status_code == 200, (
            f"tenant B must not be blocked by tenant A's definition; "
            f"got {patch_resp.status_code}: {patch_resp.text}"
        )

    # Tenant B's entity must be in state "2".
    current_state = await _get_stage_progression(
        pg_container, tenant_id=tenant_b_id, entity_id=entity_b_id
    )
    assert current_state == "2", (
        f"tenant B entity must reach '2' unblocked; got {current_state!r}"
    )
