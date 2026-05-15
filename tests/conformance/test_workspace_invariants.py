"""Workspace invariant conformance gates.

Five contract-drift guards that must hold before the workspace service
is considered shippable:

1. PII scan chokepoint — every entry write path (POST + PATCH /entries)
   invokes the PII scanner before any DB write; a scanner that raises
   propagates the failure to the HTTP response without writing a row.
2. RBAC surface — ROLE_AUDITOR == "auditor"; "auditor" is in the
   workspace router's _any_roles gate; openapi.json contains no
   /shares paths or Share* schemas; the MCP catalog has no
   list_workspace_shares tool.
3. Auditor write boundary — an auditor-only actor can perceive a
   tenant workspace but receives 403 on any write attempt.
4. Cross-tenant isolation — an actor in tenant A cannot see a
   workspace owned by tenant B regardless of the roles they hold in
   tenant A.
5. Visibility chokepoint — get_workspace is called exactly once per
   list_entries request; entry reads do not bypass the visibility
   chokepoint.

Tests in groups 1, 3, 4, 5 use real Postgres (testcontainers) and the
entitlement-resolved auth path via tests/helpers/auth_harness.py.
Tests in group 2 are pure-Python (no DB, no app boot).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

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
# Helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def harness(pg_container: str) -> AsyncIterator[EntitlementAuthHarness]:
    async with EntitlementAuthHarness(pg_container) as h:
        yield h


async def _materialise(
    h: EntitlementAuthHarness, client: AsyncClient, persona: TenantPersona
) -> None:
    """JIT-create the persona's tenant + actor row by hitting /v1/whoami."""
    h.configure_fetcher_for(persona)
    with patch_validator_for_actor(persona):
        resp = await client.get(
            "/v1/whoami", headers=bearer_headers(tenant_slug=persona.slug)
        )
        assert resp.status_code == 200, resp.text


async def _create_workspace(
    h: EntitlementAuthHarness,
    client: AsyncClient,
    persona: TenantPersona,
    *,
    name: str = "ws-conformance",
    owner_kind: str = "actor",
) -> uuid.UUID:
    h.configure_fetcher_for(persona)
    with patch_validator_for_actor(persona):
        resp = await client.post(
            "/v1/workspaces",
            json={"name": name, "owner_kind": owner_kind},
            headers=bearer_headers(tenant_slug=persona.slug),
        )
    assert resp.status_code == 201, f"create_workspace failed: {resp.status_code} {resp.text}"
    return uuid.UUID(resp.json()["workspace_id"])


async def _seed_entry_directly(
    pg_url: str, *, workspace_id: uuid.UUID, persona: TenantPersona
) -> uuid.UUID:
    """Insert one workspace_entries row by talking directly to the DB —
    used only by the PATCH-PII test where we need a row to PATCH but
    must avoid going through the (intact) PII scanner during seed."""
    entry_id = uuid.uuid4()
    engine = create_async_engine(
        pg_url, connect_args={"prepared_statement_cache_size": 0}
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            row = (
                await session.execute(
                    text("SELECT tenant_id FROM tenants WHERE slug = :slug"),
                    {"slug": persona.slug},
                )
            ).first()
            assert row is not None
            tenant_id = row[0]
            actor_row = (
                await session.execute(
                    text(
                        "SELECT actor_id FROM actors "
                        "WHERE tenant_id = :tid AND oidc_subject = :sub"
                    ),
                    {"tid": tenant_id, "sub": persona.oidc_subject},
                )
            ).first()
            assert actor_row is not None
            actor_id = actor_row[0]
            await session.execute(
                text(
                    "INSERT INTO workspace_entries "
                    "(entry_id, workspace_id, tenant_id, kind, body_md, "
                    "created_at, updated_at, created_by) "
                    "VALUES (:eid, :wid, :tid, 'note', :body, "
                    "now(), now(), :created_by)"
                ),
                {
                    "eid": entry_id,
                    "wid": workspace_id,
                    "tid": tenant_id,
                    "body": "Seed entry for invariant testing.",
                    "created_by": actor_id,
                },
            )
    finally:
        await engine.dispose()
    return entry_id


async def _count_entries(pg_url: str, *, workspace_id: uuid.UUID) -> int:
    engine = create_async_engine(
        pg_url, connect_args={"prepared_statement_cache_size": 0}
    )
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
            return int(result.scalar_one())
    finally:
        await engine.dispose()


async def _fetch_entry_body(pg_url: str, *, entry_id: uuid.UUID) -> str | None:
    engine = create_async_engine(
        pg_url, connect_args={"prepared_statement_cache_size": 0}
    )
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


class _AlwaysBombScanner:
    """PII scanner stub whose scan() always raises RuntimeError.

    Injected onto app.state.workspace_service._pii_scanner before the
    write request. Every chokepoint that calls scan() must propagate
    the failure to the HTTP response without writing a row.
    """

    def scan(self, text: str, *, field_type: str, **_kwargs: Any) -> Any:
        raise RuntimeError("_AlwaysBombScanner: unconditional PII scanner failure")


# ---------------------------------------------------------------------------
# Invariant 1 — PII scan chokepoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pii_chokepoint_blocks_create_entry(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    """Failing PII scanner must prevent INSERT on POST /entries."""
    persona = harness.add_persona(
        f"ws-pii-create-{uuid.uuid4().hex[:6]}", roles=["producer"]
    )
    transport = ASGITransport(app=harness.app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _materialise(harness, client, persona)
        workspace_id = await _create_workspace(harness, client, persona)
        before = await _count_entries(pg_container, workspace_id=workspace_id)

        # Inject the bomb scanner on the singleton workspace service.
        harness.app.state.workspace_service._pii_scanner = _AlwaysBombScanner()

        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            resp = await client.post(
                f"/v1/workspaces/{workspace_id}/entries",
                json={"kind": "note", "body_md": "This is a test entry body."},
                headers=bearer_headers(tenant_slug=persona.slug),
            )

    assert resp.status_code >= 500, (
        f"Expected 5xx when PII scanner raises; got {resp.status_code}: {resp.text}"
    )
    after = await _count_entries(pg_container, workspace_id=workspace_id)
    assert after == before, (
        f"No entry rows must be written when PII scanner raises; before={before} after={after}"
    )


@pytest.mark.asyncio
async def test_pii_chokepoint_blocks_update_entry(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    """Failing PII scanner must prevent UPDATE on PATCH /entries/{id}."""
    persona = harness.add_persona(
        f"ws-pii-update-{uuid.uuid4().hex[:6]}", roles=["producer"]
    )
    transport = ASGITransport(app=harness.app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _materialise(harness, client, persona)
        workspace_id = await _create_workspace(harness, client, persona)
        # Seed an entry directly so the (intact) scanner doesn't run during seed.
        entry_id = await _seed_entry_directly(
            pg_container, workspace_id=workspace_id, persona=persona
        )
        body_before = await _fetch_entry_body(pg_container, entry_id=entry_id)
        assert body_before is not None

        # Now arm the bomb and PATCH.
        harness.app.state.workspace_service._pii_scanner = _AlwaysBombScanner()
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            resp = await client.patch(
                f"/v1/workspaces/{workspace_id}/entries/{entry_id}",
                json={"body_md": "Attempted replacement body."},
                headers=bearer_headers(tenant_slug=persona.slug),
            )

    assert resp.status_code >= 500, (
        f"Expected 5xx on PATCH when PII scanner raises; got {resp.status_code}: {resp.text}"
    )
    body_after = await _fetch_entry_body(pg_container, entry_id=entry_id)
    assert body_after == body_before, (
        f"Body must be unchanged after a failed PATCH; before={body_before!r} after={body_after!r}"
    )


# ---------------------------------------------------------------------------
# Invariant 2 — RBAC surface (pure-Python, no DB)
# ---------------------------------------------------------------------------


def test_role_auditor_constant_exists() -> None:
    """ROLE_AUDITOR constant equals 'auditor'."""
    from registry.api.auth.context import ROLE_AUDITOR

    assert ROLE_AUDITOR == "auditor", f"Expected ROLE_AUDITOR == 'auditor'; got {ROLE_AUDITOR!r}"


def test_role_auditor_in_workspace_router_gate() -> None:
    """'auditor' is in _any_roles so auditors reach workspace read endpoints."""
    from registry.api.routers.workspaces import _any_roles

    assert "auditor" in _any_roles, (
        f"'auditor' must be in _any_roles so auditors reach workspace read endpoints; got {_any_roles!r}"
    )


def test_openapi_share_endpoints_absent() -> None:
    """openapi.json contains no /shares endpoint paths or Share* schemas."""
    import json
    from pathlib import Path

    spec_path = Path(__file__).parent.parent.parent / "openapi.json"
    spec = json.loads(spec_path.read_text())

    paths = spec.get("paths", {})
    bad_paths = [p for p in paths if "/shares" in p]
    assert not bad_paths, f"Share endpoint paths must be absent from openapi.json; found: {bad_paths}"

    schemas = spec.get("components", {}).get("schemas", {})
    bad_schemas = [s for s in schemas if "Share" in s]
    assert not bad_schemas, f"Share schemas must be absent from openapi.json; found: {bad_schemas}"


def test_mcp_share_tool_absent() -> None:
    """list_workspace_shares is not registered in the MCP tool catalog."""
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
        f"list_workspace_shares must not be registered; got tool names: {sorted(names)}"
    )


# ---------------------------------------------------------------------------
# Invariant 3 — Auditor write boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auditor_write_on_perceived_workspace_returns_403(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    """Auditor-only actor perceives a tenant workspace (200) but is
    denied 403 on POST /entries."""
    slug = f"ws-aud-write-{uuid.uuid4().hex[:6]}"
    admin = harness.add_persona(slug, roles=["admin"])
    auditor = TenantPersona(slug=slug, actor_id=uuid.uuid4(), roles=["auditor"])

    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _materialise(harness, client, admin)
        workspace_id = await _create_workspace(
            harness, client, admin, name="Auditor-visibility ws", owner_kind="tenant"
        )

        # Auditor reads — must succeed.
        harness.configure_fetcher_for(auditor)
        with patch_validator_for_actor(auditor):
            get_resp = await client.get(
                f"/v1/workspaces/{workspace_id}",
                headers=bearer_headers(tenant_slug=auditor.slug),
            )
        assert get_resp.status_code == 200, (
            f"Auditor must read tenant workspace; got {get_resp.status_code}: {get_resp.text}"
        )

        # Auditor writes — must be denied with 403.
        harness.configure_fetcher_for(auditor)
        with patch_validator_for_actor(auditor):
            write_resp = await client.post(
                f"/v1/workspaces/{workspace_id}/entries",
                json={"kind": "note", "body_md": "Auditor write attempt."},
                headers=bearer_headers(tenant_slug=auditor.slug),
            )
    assert write_resp.status_code == 403, (
        f"Auditor must receive 403 on write; got {write_resp.status_code}: {write_resp.text}"
    )


# ---------------------------------------------------------------------------
# Invariant 4 — Cross-tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_actor_in_tenant_a_cannot_see_workspace_in_tenant_b(
    harness: EntitlementAuthHarness,
) -> None:
    """An actor with full grants in tenant A cannot see a workspace
    owned by tenant B; GET /v1/workspaces/{id} returns 404 (opaque)."""
    suffix = uuid.uuid4().hex[:6]
    persona_a = harness.add_persona(
        f"ws-xten-a-{suffix}", roles=["producer", "consumer", "admin"]
    )
    persona_b = harness.add_persona(f"ws-xten-b-{suffix}", roles=["admin"])

    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _materialise(harness, client, persona_a)
        await _materialise(harness, client, persona_b)
        workspace_b_id = await _create_workspace(
            harness, client, persona_b, owner_kind="tenant"
        )

        harness.configure_fetcher_for(persona_a)
        with patch_validator_for_actor(persona_a):
            resp = await client.get(
                f"/v1/workspaces/{workspace_b_id}",
                headers=bearer_headers(tenant_slug=persona_a.slug),
            )
    # 404 is the opaque "not visible" response. 403 would also be a
    # valid denial (the tenant boundary check could surface either),
    # but never 200 — that would be a leak.
    assert resp.status_code in (403, 404), (
        f"Actor in tenant A must not see tenant B's workspace; got {resp.status_code}: {resp.text}"
    )


# ---------------------------------------------------------------------------
# Invariant 5 — Visibility chokepoint: get_workspace call count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_workspace_called_once_per_list_entries(
    harness: EntitlementAuthHarness,
) -> None:
    """get_workspace is invoked exactly once per GET /entries call.

    Wraps the singleton WorkspaceService.get_workspace with a counting
    shim. One list_entries call → counter == 1; a fast-path that
    bypassed get_workspace would leave the counter at 0.
    """
    persona = harness.add_persona(
        f"ws-vis-list-{uuid.uuid4().hex[:6]}", roles=["producer"]
    )
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _materialise(harness, client, persona)
        workspace_id = await _create_workspace(harness, client, persona)

        workspace_svc = harness.app.state.workspace_service
        call_count = 0
        original = workspace_svc.get_workspace

        async def _counting_get_workspace(ctx: Any, wid: Any) -> Any:
            nonlocal call_count
            call_count += 1
            return await original(ctx, wid)

        workspace_svc.get_workspace = _counting_get_workspace
        try:
            harness.configure_fetcher_for(persona)
            with patch_validator_for_actor(persona):
                resp = await client.get(
                    f"/v1/workspaces/{workspace_id}/entries",
                    headers=bearer_headers(tenant_slug=persona.slug),
                )
        finally:
            workspace_svc.get_workspace = original

    assert resp.status_code == 200, resp.text
    assert call_count == 1, (
        f"get_workspace must be called exactly once per list_entries; "
        f"got {call_count} (a bypass means the visibility chokepoint is leaky)"
    )


@pytest.mark.asyncio
async def test_get_workspace_called_twice_for_two_list_entries(
    harness: EntitlementAuthHarness,
) -> None:
    """The counter increments predictably — not saturated at 1, not cached out."""
    persona = harness.add_persona(
        f"ws-vis-2x-{uuid.uuid4().hex[:6]}", roles=["producer"]
    )
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _materialise(harness, client, persona)
        workspace_id = await _create_workspace(harness, client, persona)

        workspace_svc = harness.app.state.workspace_service
        call_count = 0
        original = workspace_svc.get_workspace

        async def _counting_get_workspace(ctx: Any, wid: Any) -> Any:
            nonlocal call_count
            call_count += 1
            return await original(ctx, wid)

        workspace_svc.get_workspace = _counting_get_workspace
        try:
            harness.configure_fetcher_for(persona)
            with patch_validator_for_actor(persona):
                r1 = await client.get(
                    f"/v1/workspaces/{workspace_id}/entries",
                    headers=bearer_headers(tenant_slug=persona.slug),
                )
                assert r1.status_code == 200, r1.text
                assert call_count == 1, f"after first list_entries got {call_count}"
                r2 = await client.get(
                    f"/v1/workspaces/{workspace_id}/entries",
                    headers=bearer_headers(tenant_slug=persona.slug),
                )
                assert r2.status_code == 200, r2.text
        finally:
            workspace_svc.get_workspace = original

    assert call_count == 2, (
        f"get_workspace must increment per call; got {call_count} after 2 requests"
    )
