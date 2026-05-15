"""Integration tests: regulated tenant gate for workspace and entry creation.

Tenants with is_regulated=True cannot create workspaces while the service
operates at encryption_tier='none'. The block surfaces as HTTP 422 with a
specific error message. This is a program constraint (not a bug): regulated
tenants' workspace go-live is tied to the ENC phase.

Scenarios:
- Regulated tenant POST /v1/workspaces -> 422 with standard error body.
- Same tenant with is_regulated=False -> 201.
- Defense-in-depth on create_entry: a regulated tenant that has a workspace
  row injected directly via SQL (bypassing create_workspace) is still blocked
  when it calls POST /v1/workspaces/{id}/entries -> 422 with the same body.
  This confirms the entry-layer guard is independent of the workspace-create
  guard -- both layers must fire for defense-in-depth to hold.
- Block is tenant-scoped: a different is_regulated=False tenant is unaffected.

The exact error detail string is asserted verbatim so any accidental change
to the service error message is caught immediately.
"""

from __future__ import annotations

import datetime
import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    bearer_headers,
    patch_validator_for_actor,
)

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)

# Exact error detail produced by the regulated-tenant guard in both
# WorkspaceService.create_workspace and WorkspaceService.create_entry.
_REGULATED_ERROR = (
    "Workspace creation is not permitted for regulated tenants at encryption tier 'none'. "
    "Configure a higher encryption tier before creating workspaces."
)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_tenant(
    pg_url: str,
    *,
    slug: str,
    is_regulated: bool = False,
) -> None:
    """Pre-seed a tenant row with the given is_regulated flag.

    The entitlement resolver's JIT path checks for an existing slug first;
    if the row is present it reuses the tenant_id without overwriting other
    columns. Seeding here ensures the harness-created actor lands in a
    tenant that has is_regulated already set.
    """
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO tenants (tenant_id, slug, display_name, "
                    "created_at, is_active, is_regulated) "
                    "VALUES (gen_random_uuid(), :slug, :slug, :now, TRUE, :is_reg) "
                    "ON CONFLICT (slug) DO NOTHING"
                ),
                {"slug": slug, "now": _NOW, "is_reg": is_regulated},
            )
    finally:
        await engine.dispose()


async def _inject_workspace_row(
    pg_url: str,
    *,
    workspace_id: uuid.UUID,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    owner_kind: str = "tenant",
) -> None:
    """Insert a workspace row directly via SQL, bypassing create_workspace.

    Used to simulate a data-migration or future-code-path scenario where a
    regulated tenant ends up with a workspace row in the DB without going through
    the service layer. The entry-layer guard must still fire when this tenant
    tries to create an entry.
    """
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
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
                        :now, :now, :actor_id
                    )
                    """
                ),
                {
                    "workspace_id": workspace_id,
                    "tenant_id": tenant_id,
                    "name": "injected-workspace",
                    "owner_kind": owner_kind,
                    "owner_actor_id": actor_id if owner_kind == "actor" else None,
                    "now": _NOW,
                    "actor_id": actor_id,
                },
            )
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Regulated tenant workspace create -> 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_regulated_tenant_cannot_create_workspace(pg_container: str) -> None:
    """is_regulated=True tenant receives 422 on POST /v1/workspaces with the exact error body."""
    suffix = uuid.uuid4().hex[:8]
    slug = f"ws-reg-y-{suffix}"
    # Pre-seed the tenant row with is_regulated=True before the harness JITs it.
    await _seed_tenant(pg_container, slug=slug, is_regulated=True)

    async with EntitlementAuthHarness(pg_container) as harness:
        persona = harness.add_persona(slug, roles=["admin"])
        transport = httpx.ASGITransport(app=harness.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # JIT-materialise actor (tenant row already exists with is_regulated=True).
            harness.configure_fetcher_for(persona)
            with patch_validator_for_actor(persona):
                await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))

                resp = await client.post(
                    "/v1/workspaces",
                    headers=bearer_headers(tenant_slug=slug),
                    json={"name": "regulated-ws", "owner_kind": "tenant"},
                )
    assert resp.status_code == 422, (
        f"Regulated tenant must receive 422 on workspace create; "
        f"got {resp.status_code}. Response: {resp.text}"
    )
    assert resp.json()["errors"][0]["message"] == _REGULATED_ERROR, (
        f"422 error message must match exact service error message. "
        f"Got: {resp.json()['errors'][0]['message']!r}"
    )


# ---------------------------------------------------------------------------
# Same tenant with is_regulated=False -> 201
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unregulated_tenant_can_create_workspace(pg_container: str) -> None:
    """is_regulated=False tenant successfully creates a workspace -> 201."""
    suffix = uuid.uuid4().hex[:8]
    slug = f"ws-reg-n-{suffix}"

    async with EntitlementAuthHarness(pg_container) as harness:
        persona = harness.add_persona(slug, roles=["admin"])
        transport = httpx.ASGITransport(app=harness.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            harness.configure_fetcher_for(persona)
            with patch_validator_for_actor(persona):
                await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))

                resp = await client.post(
                    "/v1/workspaces",
                    headers=bearer_headers(tenant_slug=slug),
                    json={"name": "unregulated-ws", "owner_kind": "tenant"},
                )
    assert resp.status_code == 201, (
        f"Unregulated tenant must receive 201 on workspace create; "
        f"got {resp.status_code}. Response: {resp.text}"
    )
    assert "workspace_id" in resp.json()


# ---------------------------------------------------------------------------
# Defense-in-depth: regulated tenant blocked at entry create even with injected workspace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_regulated_tenant_entry_create_blocked_independently(pg_container: str) -> None:
    """Entry-layer guard fires independently of workspace-create guard.

    A regulated tenant with a workspace row injected directly via SQL (bypassing
    create_workspace) still receives 422 on POST /v1/workspaces/{id}/entries.
    This confirms the guard in create_entry is independent and cannot be bypassed
    by any code path that manages to write a workspace row (e.g., data migration,
    future service change, or admin tool).
    """
    suffix = uuid.uuid4().hex[:8]
    slug = f"ws-di-reg-{suffix}"
    await _seed_tenant(pg_container, slug=slug, is_regulated=True)

    async with EntitlementAuthHarness(pg_container) as harness:
        persona = harness.add_persona(slug, roles=["admin"])
        transport = httpx.ASGITransport(app=harness.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            harness.configure_fetcher_for(persona)
            with patch_validator_for_actor(persona):
                r = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))
                assert r.status_code == 200
                tenant_id = uuid.UUID(r.json()["tenant_id"])
                actor_id = uuid.UUID(r.json()["actor_id"])

            # Inject a workspace row directly, bypassing the create_workspace service guard.
            injected_ws_id = uuid.uuid4()
            await _inject_workspace_row(
                pg_container,
                workspace_id=injected_ws_id,
                tenant_id=tenant_id,
                actor_id=actor_id,
                owner_kind="tenant",
            )

            # The regulated tenant now attempts to create an entry in that workspace.
            harness.configure_fetcher_for(persona)
            with patch_validator_for_actor(persona):
                entry_resp = await client.post(
                    f"/v1/workspaces/{injected_ws_id}/entries",
                    headers=bearer_headers(tenant_slug=slug),
                    json={"kind": "note", "body_md": "Defense-in-depth test entry."},
                )
    assert entry_resp.status_code == 422, (
        f"Regulated tenant must receive 422 on entry create even with an injected "
        f"workspace; got {entry_resp.status_code}. Response: {entry_resp.text}"
    )
    assert entry_resp.json()["errors"][0]["message"] == _REGULATED_ERROR, (
        f"Entry-layer 422 error message must match the same error message as workspace-create. "
        f"Got: {entry_resp.json()['errors'][0]['message']!r}"
    )


# ---------------------------------------------------------------------------
# Block is tenant-scoped: unregulated tenant in same run is unaffected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_regulated_block_does_not_affect_other_tenants(pg_container: str) -> None:
    """Block is tenant-scoped: an unregulated tenant can create workspaces and entries.

    Confirms that the is_regulated check is read from the calling tenant's row,
    not from a global flag or a shared state that could incorrectly propagate
    to other tenants.
    """
    suffix = uuid.uuid4().hex[:8]
    slug_reg = f"ws-scope-reg-{suffix}"
    slug_ok = f"ws-scope-ok-{suffix}"

    await _seed_tenant(pg_container, slug=slug_reg, is_regulated=True)
    # slug_ok is not pre-seeded -- JIT creates it as non-regulated (default).

    async with EntitlementAuthHarness(pg_container) as harness:
        persona_reg = harness.add_persona(slug_reg, roles=["admin"])
        persona_ok = harness.add_persona(slug_ok, roles=["admin"])
        transport = httpx.ASGITransport(app=harness.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            harness.configure_fetcher_for(persona_reg)
            with patch_validator_for_actor(persona_reg):
                await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug_reg))

            harness.configure_fetcher_for(persona_ok)
            with patch_validator_for_actor(persona_ok):
                await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug_ok))

            # Regulated tenant -> 422.
            harness.configure_fetcher_for(persona_reg)
            with patch_validator_for_actor(persona_reg):
                reg_resp = await client.post(
                    "/v1/workspaces",
                    headers=bearer_headers(tenant_slug=slug_reg),
                    json={"name": "should-fail", "owner_kind": "tenant"},
                )
            assert reg_resp.status_code == 422, (
                f"Regulated tenant must be blocked; got {reg_resp.status_code}"
            )

            # Unregulated tenant -> 201.
            harness.configure_fetcher_for(persona_ok)
            with patch_validator_for_actor(persona_ok):
                ok_resp = await client.post(
                    "/v1/workspaces",
                    headers=bearer_headers(tenant_slug=slug_ok),
                    json={"name": "should-succeed", "owner_kind": "tenant"},
                )
                assert ok_resp.status_code == 201, (
                    f"Unregulated tenant must succeed; got {ok_resp.status_code}. "
                    f"Response: {ok_resp.text}"
                )
                ws_id = ok_resp.json()["workspace_id"]

                # Unregulated tenant can also create entries.
                entry_resp = await client.post(
                    f"/v1/workspaces/{ws_id}/entries",
                    headers=bearer_headers(tenant_slug=slug_ok),
                    json={"kind": "note", "body_md": "Entry in unregulated workspace."},
                )
                assert entry_resp.status_code == 201, (
                    f"Unregulated tenant must be able to create entries; "
                    f"got {entry_resp.status_code}. Response: {entry_resp.text}"
                )
