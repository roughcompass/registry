"""Workspace invariant conformance gates.

Five contract-drift guards that must hold before the workspace service is
considered shippable:

1. PII scan chokepoint — every entry write path (create_entry, update_entry)
   invokes the PII scanner before any DB write, and a scanner that raises
   unconditionally propagates the failure to the HTTP response without writing
   a row.

2. RBAC surface — ROLE_AUDITOR constant equals "auditor"; "auditor" is included
   in the _any_roles gate in the workspace router; the openapi.json snapshot
   contains no /shares paths; the MCP server does not register list_workspace_shares.

3. Auditor write boundary — an actor whose only role is "auditor" can read
   (perceive) a workspace in their own tenant but receives 403 on any write attempt
   (POST /entries). This is the role-boundary test for the auditor read-only
   contract.

4. Cross-tenant isolation — role-based, not share-based. An actor in tenant A
   cannot see a workspace owned by tenant B, regardless of what roles the actor
   holds in tenant A.

5. Workspace visibility chokepoint — get_workspace is called exactly once when
   list_entries is called for a visible workspace (counter == 1). The chokepoint
   must not be bypassable on the entry read path.

Tests in groups 1, 3, 4, 5 use real Postgres (testcontainers) from the
session-scoped pg_container fixture in tests/conftest.py. Tests in group 2 are
pure-Python (no DB required).
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
    """Insert (tenant, actor, api_token, roles, actor_roles). Returns (tenant_id, actor_id, raw_token).

    Seeds both api_tokens.roles[] (token-auth surface) and actor_roles rows (workspace
    RBAC surface). Both must be consistent: the workspace service reads actor_roles, not
    api_tokens.roles, for authorization decisions.
    """
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
            # Seed roles and actor_roles so _load_effective_roles returns the
            # correct set. The workspace service reads actor_roles JOIN roles,
            # not api_tokens.roles[], for authorization decisions.
            for role_name in role_list:
                role_id = uuid.uuid4()
                await session.execute(
                    text(
                        "INSERT INTO roles (role_id, tenant_id, name, permissions, created_at) "
                        "VALUES (:rid, :tid, :name, '{}', :now) "
                        "ON CONFLICT DO NOTHING"
                    ),
                    {"rid": role_id, "tid": tenant_id, "name": role_name, "now": _NOW},
                )
                # Re-query to get the actual role_id (ON CONFLICT DO NOTHING may skip insert).
                row = await session.execute(
                    text("SELECT role_id FROM roles WHERE tenant_id = :tid AND name = :name"),
                    {"tid": tenant_id, "name": role_name},
                )
                actual_role_id = row.scalar_one()
                await session.execute(
                    text(
                        "INSERT INTO actor_roles (tenant_id, actor_id, role_id, granted_at) "
                        "VALUES (:tid, :aid, :rid, :now) ON CONFLICT DO NOTHING"
                    ),
                    {"tid": tenant_id, "aid": actor_id, "rid": actual_role_id, "now": _NOW},
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
                    "SELECT COUNT(*) FROM workspace_entries " "WHERE workspace_id = :wid AND t_invalidated_at IS NULL"
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
    tenant_id, actor_id, token = await _seed_tenant_with_token(pg_container, slug=f"ws-pii-create-{suffix}")
    workspace_id = await _seed_workspace(pg_container, tenant_id=tenant_id, actor_id=actor_id, owner_kind="actor")

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

    assert resp.status_code >= 500, f"Expected 5xx when PII scanner raises; got {resp.status_code}: {resp.text}"

    after_count = await _count_entries(pg_container, workspace_id=workspace_id)
    assert after_count == before_count, (
        f"No entry rows must be written when PII scanner raises; " f"before={before_count} after={after_count}"
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
    tenant_id, actor_id, token = await _seed_tenant_with_token(pg_container, slug=f"ws-pii-update-{suffix}")
    workspace_id = await _seed_workspace(pg_container, tenant_id=tenant_id, actor_id=actor_id, owner_kind="actor")
    entry_id = await _seed_entry(pg_container, workspace_id=workspace_id, tenant_id=tenant_id, actor_id=actor_id)

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

    assert (
        resp.status_code >= 500
    ), f"Expected 5xx when PII scanner raises on update; got {resp.status_code}: {resp.text}"

    body_after = await _fetch_entry_body(pg_container, entry_id=entry_id)
    assert body_after == body_before, (
        f"Entry body must be unchanged when PII scanner raises; " f"before={body_before!r} after={body_after!r}"
    )


# ---------------------------------------------------------------------------
# Invariant 2 — RBAC surface: pure-Python static assertions
# ---------------------------------------------------------------------------


def test_role_auditor_constant_exists() -> None:
    """ROLE_AUDITOR constant equals 'auditor'."""
    from registry.api.auth.context import ROLE_AUDITOR

    assert ROLE_AUDITOR == "auditor", f"Expected ROLE_AUDITOR == 'auditor'; got {ROLE_AUDITOR!r}"


def test_role_auditor_in_workspace_router_gate() -> None:
    """ROLE_AUDITOR ('auditor') is included in _any_roles so auditors reach read endpoints."""
    from registry.api.routers.workspaces import _any_roles

    assert "auditor" in _any_roles, (
        f"'auditor' must be in _any_roles so auditors reach workspace read endpoints; " f"got {_any_roles!r}"
    )


def test_openapi_share_endpoints_absent() -> None:
    """openapi.json contains no /shares endpoint paths or Share* schemas."""
    import json
    from pathlib import Path

    spec_path = Path(__file__).parent.parent.parent / "openapi.json"
    spec = json.loads(spec_path.read_text())

    paths = spec.get("paths", {})
    bad_paths = [p for p in paths if "/shares" in p]
    assert not bad_paths, (
        f"Share endpoint paths must be absent from openapi.json after WRB migration; " f"found: {bad_paths}"
    )

    schemas = spec.get("components", {}).get("schemas", {})
    bad_schemas = [s for s in schemas if "Share" in s]
    assert not bad_schemas, f"Share schemas must be absent from openapi.json; found: {bad_schemas}"


def test_mcp_share_tool_absent() -> None:
    """list_workspace_shares is not registered in the MCP tool catalog."""
    import asyncio
    from unittest.mock import MagicMock

    from registry.api.routers.mcp import create_registry_mcp_server

    server = create_registry_mcp_server(
        retrieval=MagicMock(),
        catalog=MagicMock(),
        session_factory=MagicMock(),
        annotation_service=MagicMock(),
        workspace_service=MagicMock(),
    )
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert "list_workspace_shares" not in names, (
        f"list_workspace_shares must not be registered in the MCP tool catalog; " f"found tool names: {sorted(names)}"
    )


# ---------------------------------------------------------------------------
# Invariant 3 — Auditor write boundary: auditor can read, not write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auditor_write_on_perceived_workspace_returns_403(
    pg_container: str,
    app_settings: Settings,
) -> None:
    """An auditor who can perceive a tenant workspace receives 403 on write (POST /entries).

    The auditor role is a read-only role. Perceiving the workspace (GET returns 200)
    does not grant write access. POST /entries on a tenant workspace by a caller whose
    only role is 'auditor' must return 403.
    """
    suffix = uuid.uuid4().hex[:6]
    # Admin creates a tenant-owned workspace that the auditor can perceive.
    admin_tid, admin_aid, admin_token = await _seed_tenant_with_token(
        pg_container, slug=f"ws-aud-write-{suffix}", roles=["admin"]
    )
    # Auditor in the same tenant.
    _, _, auditor_token = await _seed_tenant_with_token(pg_container, slug=f"ws-aud-actor-{suffix}", roles=["auditor"])
    # The auditor must share the same tenant_id as the admin to perceive the workspace.
    # _seed_tenant_with_token creates a new tenant each call, so we create the auditor
    # actor directly in admin_tid and seed the auditor's actor_roles in that tenant.
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    auditor_actor_id = uuid.uuid4()
    raw_auditor_token = secrets.token_urlsafe(24)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, display_name, created_at) "
                    "VALUES (:aid, :tid, 'auditor-actor', :now)"
                ),
                {"aid": auditor_actor_id, "tid": admin_tid, "now": _NOW},
            )
            await session.execute(
                text(
                    "INSERT INTO api_tokens "
                    "(token_id, tenant_id, actor_id, token_hash, roles, created_at) "
                    "VALUES (gen_random_uuid(), :tid, :aid, :th, :roles, :now)"
                ),
                {
                    "tid": admin_tid,
                    "aid": auditor_actor_id,
                    "th": hash_token(raw_auditor_token),
                    "roles": ["auditor"],
                    "now": _NOW,
                },
            )
            # Ensure 'auditor' role row exists in admin's tenant, then link actor.
            auditor_role_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO roles (role_id, tenant_id, name, permissions, created_at) "
                    "VALUES (:rid, :tid, 'auditor', '{}', :now) ON CONFLICT DO NOTHING"
                ),
                {"rid": auditor_role_id, "tid": admin_tid, "now": _NOW},
            )
            row = await session.execute(
                text("SELECT role_id FROM roles WHERE tenant_id = :tid AND name = 'auditor'"),
                {"tid": admin_tid},
            )
            actual_role_id = row.scalar_one()
            await session.execute(
                text(
                    "INSERT INTO actor_roles (tenant_id, actor_id, role_id, granted_at) "
                    "VALUES (:tid, :aid, :rid, :now) ON CONFLICT DO NOTHING"
                ),
                {"tid": admin_tid, "aid": auditor_actor_id, "rid": actual_role_id, "now": _NOW},
            )
    finally:
        await engine.dispose()

    app = create_app(app_settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Admin creates a tenant-owned workspace (admin token has roles seeded in actor_roles).
        create_resp = await client.post(
            "/v1/workspaces",
            json={"name": "Auditor-visibility workspace", "owner_kind": "tenant"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert (
            create_resp.status_code == 201
        ), f"Admin must be able to create tenant workspace; got {create_resp.status_code}: {create_resp.text}"
        workspace_id = create_resp.json()["workspace_id"]

        # Auditor reads the workspace — must succeed (auditor can perceive tenant workspaces).
        get_resp = await client.get(
            f"/v1/workspaces/{workspace_id}",
            headers={"Authorization": f"Bearer {raw_auditor_token}"},
        )
        assert get_resp.status_code == 200, (
            f"Auditor must be able to read a tenant workspace (perceive=true); "
            f"got {get_resp.status_code}: {get_resp.text}"
        )

        # Auditor attempts to write an entry — role gate must deny with 403.
        write_resp = await client.post(
            f"/v1/workspaces/{workspace_id}/entries",
            json={"kind": "note", "body_md": "Auditor write attempt."},
            headers={"Authorization": f"Bearer {raw_auditor_token}"},
        )
        assert write_resp.status_code == 403, (
            f"Auditor must receive 403 on write attempt (not 404, not 201); "
            f"got {write_resp.status_code}: {write_resp.text}"
        )


# ---------------------------------------------------------------------------
# Invariant 4 — Cross-tenant isolation: role-based, not share-based
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_actor_in_tenant_a_cannot_see_workspace_in_tenant_b(
    pg_container: str,
    app_settings: Settings,
) -> None:
    """An actor in tenant A cannot access a workspace owned by tenant B.

    Cross-tenant isolation is enforced by the role-based visibility predicate.
    The actor holds producer + consumer + admin roles in tenant A, but those
    roles are scoped to tenant A. They grant no access to workspaces in tenant B.
    GET /v1/workspaces/{id} must return 404 (opaque not-found, not 403).
    """
    suffix = uuid.uuid4().hex[:6]
    # Tenant A — actor will try to access tenant B's workspace.
    _, _, token_a = await _seed_tenant_with_token(
        pg_container, slug=f"ws-xten-a-{suffix}", roles=["producer", "consumer", "admin"]
    )
    # Tenant B — workspace lives here.
    tenant_b_id, actor_b_id, _ = await _seed_tenant_with_token(
        pg_container, slug=f"ws-xten-b-{suffix}", roles=["admin"]
    )
    workspace_b_id = await _seed_workspace(
        pg_container, tenant_id=tenant_b_id, actor_id=actor_b_id, owner_kind="tenant"
    )

    app = create_app(app_settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/v1/workspaces/{workspace_b_id}",
            headers={"Authorization": f"Bearer {token_a}"},
        )

    # Must receive 404 — the workspace is opaque to callers in other tenants.
    assert resp.status_code == 404, (
        f"Actor in tenant A must not be able to see a workspace in tenant B; "
        f"expected 404 but got {resp.status_code}: {resp.text}"
    )


# ---------------------------------------------------------------------------
# Invariant 5 — Workspace visibility chokepoint: get_workspace call count
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
    tenant_id, actor_id, token = await _seed_tenant_with_token(pg_container, slug=f"ws-vis-list-{suffix}")
    workspace_id = await _seed_workspace(pg_container, tenant_id=tenant_id, actor_id=actor_id, owner_kind="actor")

    app = create_app(app_settings)
    workspace_svc = app.state.workspace_service

    call_count = 0
    _original_get_workspace = workspace_svc.get_workspace

    async def _counting_get_workspace(ctx: Any, workspace_id_arg: Any) -> Any:
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
        f"list_entries must return 200 for an authorized caller; " f"got {resp.status_code}: {resp.text}"
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
    tenant_id, actor_id, token = await _seed_tenant_with_token(pg_container, slug=f"ws-vis-2x-{suffix}")
    workspace_id = await _seed_workspace(pg_container, tenant_id=tenant_id, actor_id=actor_id, owner_kind="actor")

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
        assert resp1.status_code == 200, f"First list_entries must return 200; got {resp1.status_code}: {resp1.text}"
        assert call_count == 1, (
            f"get_workspace must be called exactly once after first list_entries; " f"got call_count={call_count}"
        )

        resp2 = await client.get(
            f"/v1/workspaces/{workspace_id}/entries",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp2.status_code == 200, f"Second list_entries must return 200; got {resp2.status_code}: {resp2.text}"
        assert call_count == 2, (
            f"get_workspace must be called exactly once per list_entries call; "
            f"got call_count={call_count} after second request"
        )
