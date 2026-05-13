"""Workspace invariant conformance gates.

Three contract-drift guards that must hold before the workspace service is
considered shippable:

1. PII scan chokepoint — every entry write path (create_entry, update_entry)
   invokes the PII scanner before any DB write, and a scanner that raises
   unconditionally propagates the failure to the HTTP response without writing
   a row.

2. Owner_kind cross-tenant share rule (DB trigger backstop) — a direct INSERT
   into workspace_shares for an actor-owned workspace where
   grantee_tenant_id != workspace.tenant_id must be rejected by the
   trg_ws_share_cross_tenant BEFORE INSERT trigger with a message containing
   'cross-tenant share rejected'. This tests the DB-level enforcement independently
   of the service-layer guard.

3. Workspace visibility chokepoint — get_workspace is called exactly once when
   list_entries is called for a visible workspace (counter == 1). The chokepoint
   must not be bypassable on the entry read path.

Tests use real Postgres (testcontainers) from the session-scoped pg_container
fixture in tests/conftest.py. The PII chokepoint invariants use ASGITransport
with app.state.pii_scanner overridden to a bomb scanner.
"""

from __future__ import annotations

import datetime
import secrets
import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.api.auth.tokens import hash_token
from registry.config import Settings
from registry.main import create_app

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_tenant_with_token(
    pg_url: str,
    *,
    slug: str,
    roles: list[str] | None = None,
) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Insert (tenant, actor, api_token). Returns (tenant_id, actor_id, raw_token)."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    raw_token = secrets.token_urlsafe(24)
    role_list = roles or ["producer", "consumer", "admin"]
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO tenants (tenant_id, slug, display_name, "
                    "created_at, is_active) VALUES "
                    "(:tid, :slug, :slug, :now, TRUE)"
                ),
                {"tid": tenant_id, "slug": slug, "now": _NOW},
            )
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, display_name, "
                    "created_at) VALUES (:aid, :tid, :dn, :now)"
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
                    "roles": role_list,
                    "now": _NOW,
                },
            )
    finally:
        await engine.dispose()
    return tenant_id, actor_id, raw_token


async def _seed_workspace(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    owner_kind: str = "actor",
) -> uuid.UUID:
    """Insert a workspace row directly. Returns workspace_id."""
    workspace_id = uuid.uuid4()
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owner_actor_id = actor_id if owner_kind == "actor" else None
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    """
                    INSERT INTO workspaces (
                        workspace_id, tenant_id, name, description,
                        owner_kind, owner_actor_id, encryption_tier,
                        created_at, updated_at, created_by
                    ) VALUES (
                        :workspace_id, :tenant_id, :name, NULL,
                        :owner_kind, :owner_actor_id, 'none',
                        :now, :now, :created_by
                    )
                    """
                ),
                {
                    "workspace_id": workspace_id,
                    "tenant_id": tenant_id,
                    "name": f"ws-{workspace_id.hex[:8]}",
                    "owner_kind": owner_kind,
                    "owner_actor_id": owner_actor_id,
                    "now": _NOW,
                    "created_by": actor_id,
                },
            )
    finally:
        await engine.dispose()
    return workspace_id


async def _seed_entry(
    pg_url: str,
    *,
    workspace_id: uuid.UUID,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> uuid.UUID:
    """Insert a workspace_entries row directly. Returns entry_id."""
    entry_id = uuid.uuid4()
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    """
                    INSERT INTO workspace_entries (
                        entry_id, workspace_id, tenant_id, kind, body_md,
                        references_jsonb, reference_ids,
                        expires_at, created_at, updated_at, created_by
                    ) VALUES (
                        :entry_id, :workspace_id, :tenant_id, 'note', :body,
                        NULL, '{}',
                        NULL, :now, :now, :created_by
                    )
                    """
                ),
                {
                    "entry_id": entry_id,
                    "workspace_id": workspace_id,
                    "tenant_id": tenant_id,
                    "body": "Seed entry for invariant testing.",
                    "now": _NOW,
                    "created_by": actor_id,
                },
            )
    finally:
        await engine.dispose()
    return entry_id


async def _count_entries(
    pg_url: str,
    *,
    workspace_id: uuid.UUID,
) -> int:
    """Count active (non-soft-deleted) entry rows for a workspace."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await session.execute(
                text(
                    "SELECT COUNT(*) FROM workspace_entries "
                    "WHERE workspace_id = :wid AND t_invalidated_at IS NULL"
                ),
                {"wid": workspace_id},
            )
            return result.scalar_one()
    finally:
        await engine.dispose()


async def _fetch_entry_body(
    pg_url: str,
    *,
    entry_id: uuid.UUID,
) -> str | None:
    """Return the current body_md of an entry row, or None if not found."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await session.execute(
                text("SELECT body_md FROM workspace_entries WHERE entry_id = :eid"),
                {"eid": entry_id},
            )
            row = result.fetchone()
            return row[0] if row else None
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Failing PII scanner stub
# ---------------------------------------------------------------------------


class _AlwaysBombScanner:
    """PIIScanner stub whose scan() always raises RuntimeError.

    Injected into app.state.pii_scanner before the request so the HTTP handler
    receives the exception. Every write path that reaches the PII chokepoint
    fails immediately — no DB row must be written.
    """

    def scan(self, text: str, *, field_type: str, **_kwargs: Any) -> Any:
        raise RuntimeError("_AlwaysBombScanner: unconditional PII scanner failure")


# ---------------------------------------------------------------------------
# Invariant 1 — PII scan chokepoint: create_entry path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pii_chokepoint_blocks_create_entry(
    pg_container: str,
    app_settings: Settings,
) -> None:
    """A failing PII scanner must prevent INSERT on POST /v1/workspaces/{id}/entries.

    The scanner is injected via app.state.pii_scanner before the workspace service
    singleton is built, so the singleton receives the bomb scanner. The request must
    fail with 5xx, and zero entry rows must be written to the DB after the failure.
    """
    suffix = uuid.uuid4().hex[:6]
    tenant_id, actor_id, token = await _seed_tenant_with_token(
        pg_container, slug=f"ws-pii-create-{suffix}"
    )
    workspace_id = await _seed_workspace(
        pg_container, tenant_id=tenant_id, actor_id=actor_id, owner_kind="actor"
    )

    before_count = await _count_entries(pg_container, workspace_id=workspace_id)

    app = create_app(app_settings)
    # Inject the bomb scanner before the workspace service singleton is built.
    # _build_workspace_service reads app.state.pii_scanner; the singleton is built
    # inside create_app so it must be overridden before the request is sent.
    app.state.workspace_service._pii_scanner = _AlwaysBombScanner()

    # raise_app_exceptions=False lets the transport convert the unhandled
    # RuntimeError into a 500 response — what we want to assert is that the
    # HTTP contract fails, not which exception propagated.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/v1/workspaces/{workspace_id}/entries",
            json={"kind": "note", "body_md": "This is a test entry body."},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code >= 500, (
        f"Expected 5xx when PII scanner raises; got {resp.status_code}: {resp.text}"
    )

    after_count = await _count_entries(pg_container, workspace_id=workspace_id)
    assert after_count == before_count, (
        f"No entry rows must be written when PII scanner raises; "
        f"before={before_count} after={after_count}"
    )


# ---------------------------------------------------------------------------
# Invariant 1 — PII scan chokepoint: update_entry path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pii_chokepoint_blocks_update_entry(
    pg_container: str,
    app_settings: Settings,
) -> None:
    """A failing PII scanner must prevent UPDATE on PATCH /v1/workspaces/{id}/entries/{eid}.

    Setup: seed a workspace and one entry directly (bypassing the service so the
    healthy PII scanner is not involved). Then inject the bomb scanner and PATCH
    with a new body_md. The PATCH must fail with 5xx and the entry body must be
    unchanged in the DB.
    """
    suffix = uuid.uuid4().hex[:6]
    tenant_id, actor_id, token = await _seed_tenant_with_token(
        pg_container, slug=f"ws-pii-update-{suffix}"
    )
    workspace_id = await _seed_workspace(
        pg_container, tenant_id=tenant_id, actor_id=actor_id, owner_kind="actor"
    )
    entry_id = await _seed_entry(
        pg_container, workspace_id=workspace_id, tenant_id=tenant_id, actor_id=actor_id
    )

    body_before = await _fetch_entry_body(pg_container, entry_id=entry_id)
    assert body_before is not None

    app = create_app(app_settings)
    # Swap out the PII scanner on the already-built singleton.
    app.state.workspace_service._pii_scanner = _AlwaysBombScanner()

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.patch(
            f"/v1/workspaces/{workspace_id}/entries/{entry_id}",
            json={"body_md": "Attempted replacement body."},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code >= 500, (
        f"Expected 5xx when PII scanner raises on update; got {resp.status_code}: {resp.text}"
    )

    body_after = await _fetch_entry_body(pg_container, entry_id=entry_id)
    assert body_after == body_before, (
        f"Entry body must be unchanged when PII scanner raises; "
        f"before={body_before!r} after={body_after!r}"
    )


# ---------------------------------------------------------------------------
# Invariant 2 — Owner_kind cross-tenant share rule: DB trigger backstop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_tenant_share_trigger_backstop(
    pg_container: str,
    app_settings: Settings,
) -> None:
    """The trg_ws_share_cross_tenant trigger rejects cross-tenant shares on actor-owned workspaces.

    A direct INSERT into workspace_shares for an actor-owned workspace where
    grantee_tenant_id differs from the workspace's owning tenant must be rejected
    by the DB trigger with an error message containing 'cross-tenant share rejected'.

    This test exercises Layer 1 (the DB trigger) independently of Layer 2
    (the service-layer guard in grant_share). Both layers must independently
    enforce the rule so that a future service-layer change cannot silently open
    a cross-tenant share gap.
    """
    suffix = uuid.uuid4().hex[:6]
    # Owning tenant — actor-owned workspace lives here.
    tenant_id, actor_id, _token = await _seed_tenant_with_token(
        pg_container, slug=f"ws-trig-owner-{suffix}"
    )
    # Grantee tenant — a distinct tenant; cross-tenant share attempt.
    grantee_tenant_id, grantee_actor_id, _grantee_token = await _seed_tenant_with_token(
        pg_container, slug=f"ws-trig-grantee-{suffix}"
    )
    workspace_id = await _seed_workspace(
        pg_container, tenant_id=tenant_id, actor_id=actor_id, owner_kind="actor"
    )

    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)

    caught_exc: Exception | None = None
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    """
                    INSERT INTO workspace_shares (
                        share_id, workspace_id, tenant_id,
                        grantee_actor_id, grantee_tenant_id,
                        role, granted_at
                    ) VALUES (
                        gen_random_uuid(), :workspace_id, :tenant_id,
                        :grantee_actor_id, :grantee_tenant_id,
                        'reader', :now
                    )
                    """
                ),
                {
                    "workspace_id": workspace_id,
                    "tenant_id": tenant_id,
                    "grantee_actor_id": grantee_actor_id,
                    "grantee_tenant_id": grantee_tenant_id,
                    "now": _NOW,
                },
            )
    except Exception as exc:
        caught_exc = exc
    finally:
        await engine.dispose()

    # The trigger must have fired — if no exception, the backstop is absent.
    assert caught_exc is not None, (
        "Expected a DB-level exception for a cross-tenant share on an actor-owned workspace; "
        "no exception was raised. The trigger trg_ws_share_cross_tenant may be missing "
        "or incorrectly scoped."
    )

    # The trigger message must identify the rule that was violated.
    err_str = str(caught_exc).lower()
    assert "cross-tenant share rejected" in err_str, (
        f"Trigger error message must contain 'cross-tenant share rejected'; "
        f"got: {caught_exc!r}"
    )


# ---------------------------------------------------------------------------
# Invariant 3 — Workspace visibility chokepoint: get_workspace call count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_workspace_called_once_per_list_entries(
    pg_container: str,
    app_settings: Settings,
) -> None:
    """get_workspace is called exactly once when list_entries is called.

    Wraps get_workspace on the singleton WorkspaceService with a counting shim
    that preserves the original coroutine. Calls GET /v1/workspaces/{id}/entries
    once; asserts the counter is exactly 1. This confirms that every entry read
    path funnels through the workspace visibility chokepoint without any fast-path
    that skips it.
    """
    suffix = uuid.uuid4().hex[:6]
    tenant_id, actor_id, token = await _seed_tenant_with_token(
        pg_container, slug=f"ws-vis-list-{suffix}"
    )
    workspace_id = await _seed_workspace(
        pg_container, tenant_id=tenant_id, actor_id=actor_id, owner_kind="actor"
    )

    app = create_app(app_settings)
    workspace_svc = app.state.workspace_service

    call_count = 0
    _original_get_workspace = workspace_svc.get_workspace

    async def _counting_get_workspace(
        ctx: Any, workspace_id_arg: Any
    ) -> Any:
        nonlocal call_count
        call_count += 1
        return await _original_get_workspace(ctx, workspace_id_arg)

    # Replace the method on the singleton — all requests through this app instance
    # will use the counting wrapper.
    workspace_svc.get_workspace = _counting_get_workspace  # type: ignore[method-assign]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/v1/workspaces/{workspace_id}/entries",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200, (
        f"list_entries must return 200 for an authorized caller; "
        f"got {resp.status_code}: {resp.text}"
    )
    assert call_count == 1, (
        f"get_workspace must be called exactly once per list_entries call; "
        f"got call_count={call_count}. A fast-path may be bypassing the visibility chokepoint."
    )


@pytest.mark.asyncio
async def test_get_workspace_called_once_per_second_list_entries(
    pg_container: str,
    app_settings: Settings,
) -> None:
    """get_workspace call count increments predictably with multiple list_entries calls.

    Calls GET /v1/workspaces/{id}/entries twice in sequence; asserts the counter
    reaches exactly 2. This confirms that the counter is not saturating at 1 or
    short-circuiting after the first request (e.g. due to caching on the service
    singleton).
    """
    suffix = uuid.uuid4().hex[:6]
    tenant_id, actor_id, token = await _seed_tenant_with_token(
        pg_container, slug=f"ws-vis-2x-{suffix}"
    )
    workspace_id = await _seed_workspace(
        pg_container, tenant_id=tenant_id, actor_id=actor_id, owner_kind="actor"
    )

    app = create_app(app_settings)
    workspace_svc = app.state.workspace_service

    call_count = 0
    _original_get_workspace = workspace_svc.get_workspace

    async def _counting_get_workspace(ctx: Any, workspace_id_arg: Any) -> Any:
        nonlocal call_count
        call_count += 1
        return await _original_get_workspace(ctx, workspace_id_arg)

    workspace_svc.get_workspace = _counting_get_workspace  # type: ignore[method-assign]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp1 = await client.get(
            f"/v1/workspaces/{workspace_id}/entries",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp1.status_code == 200, (
            f"First list_entries must return 200; got {resp1.status_code}: {resp1.text}"
        )
        assert call_count == 1, (
            f"get_workspace must be called exactly once after first list_entries; "
            f"got call_count={call_count}"
        )

        resp2 = await client.get(
            f"/v1/workspaces/{workspace_id}/entries",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp2.status_code == 200, (
            f"Second list_entries must return 200; got {resp2.status_code}: {resp2.text}"
        )
        assert call_count == 2, (
            f"get_workspace must be called exactly once per list_entries call; "
            f"got call_count={call_count} after second request"
        )
