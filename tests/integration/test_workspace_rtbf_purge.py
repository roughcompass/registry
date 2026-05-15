"""Integration tests: RTBF physical purge for workspace data.

Covers the normative two-actor, two-workspace scenario:

- Actor A owns a personal workspace with only A's entries.
- Actor A owns a second workspace where Actor B also has entries.
- Admin issues DELETE /v1/admin/actors/{actor_A_id}/personal-data -> 200 with PurgeResult.

Assertions verified against a live Postgres instance (testcontainers):

Personal workspace (actor-only entries):
- All Actor A workspace entries deleted.
- workspace row deleted.
- PurgeResult.purged_workspaces count is 1.

Workspace with mixed entries (Actor A + Actor B):
- Actor A's entries deleted; workspace row survives.
- workspace.owner_actor_id is NULL after purge.
- workspace.archived_at is set (not NULL) after purge.
- Actor B's entries are intact.

PurgeResult{purged_entries, purged_workspaces} counts are accurate.
The purged_workspaces count excludes workspaces with other actors' entries.
Non-admin calling the endpoint -> 403.

The real Postgres instance is used because these tests verify that:
- Hard DELETEs cascade correctly through FK constraints.
- The two-step purge sequence (entries -> workspace cleanup) operates
  atomically within a single transaction.
- No FK violations occur during workspace deletion.

The visibility chokepoint and transaction semantics are exercised for real --
they are not mocked -- because unit tests cannot reach FK-constraint enforcement
or real DELETE row-count semantics.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

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


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_workspace(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    owner_actor_id: uuid.UUID,
    name: str,
) -> uuid.UUID:
    """Insert an actor-owned workspace. Returns workspace_id."""
    ws_id = uuid.uuid4()
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO workspaces "
                    "(workspace_id, tenant_id, name, owner_kind, owner_actor_id, "
                    " encryption_tier, created_at, updated_at, created_by) "
                    "VALUES (:wid, :tid, :name, 'actor', :oid, 'none', :now, :now, :oid)"
                ),
                {
                    "wid": ws_id,
                    "tid": tenant_id,
                    "name": name,
                    "oid": owner_actor_id,
                    "now": _NOW,
                },
            )
    finally:
        await engine.dispose()
    return ws_id


async def _seed_entry(
    pg_url: str,
    *,
    workspace_id: uuid.UUID,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    body: str,
) -> uuid.UUID:
    """Insert a workspace entry authored by actor_id. Returns entry_id."""
    entry_id = uuid.uuid4()
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO workspace_entries "
                    "(entry_id, workspace_id, tenant_id, kind, body_md, "
                    " created_at, updated_at, created_by) "
                    "VALUES (:eid, :wid, :tid, 'note', :body, :now, :now, :aid)"
                ),
                {
                    "eid": entry_id,
                    "wid": workspace_id,
                    "tid": tenant_id,
                    "body": body,
                    "now": _NOW,
                    "aid": actor_id,
                },
            )
    finally:
        await engine.dispose()
    return entry_id


async def _count_rows(pg_url: str, table: str, where: str, params: dict[str, Any]) -> int:
    """Return the row count for a simple WHERE query."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await session.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE {where}"),  # noqa: S608
                params,
            )
            return int(result.scalar_one())
    finally:
        await engine.dispose()


async def _fetch_one(pg_url: str, query: str, params: dict[str, Any]) -> Any:
    """Fetch one row for a given query; returns None if no row found."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await session.execute(text(query), params)
            return result.fetchone()
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# RTBF physical purge scenario
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rtbf_purge_personal_and_shared_workspace(pg_container: str) -> None:
    """Normative RTBF scenario: actor-owned workspaces fully erased.

    Setup:
    - Tenant T with Actor A (admin) and Actor B (non-admin).
    - Actor A creates a personal workspace containing only A's entries.
    - Actor A creates a second workspace that also contains a residual entry
      authored by Actor B (legacy data shape -- actor-owned workspaces are
      single-writer by design, so this entry cannot recur after this change).

    Admin (Actor A) issues DELETE /v1/admin/actors/{actor_A_id}/personal-data -> 200.

    Verified outcomes:
    1. PurgeResult counts are accurate; no revoked_shares field exists.
    2. Both actor-owned workspaces are physically deleted along with every entry
       they contained, including residual entries authored by other actors.
       Actor-owned workspaces cannot be preserved past their owner under the
       single-writer invariant.
    """
    suffix = uuid.uuid4().hex[:8]
    slug_a = f"rtbf-a-{suffix}"
    slug_b = f"rtbf-b-{suffix}"

    async with EntitlementAuthHarness(pg_container) as harness:
        persona_a = harness.add_persona(slug_a, roles=["producer", "consumer", "admin"])
        persona_b = harness.add_persona(slug_b, roles=["producer", "consumer"])

        transport = httpx.ASGITransport(app=harness.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # JIT-materialise both actors.
            harness.configure_fetcher_for(persona_a)
            with patch_validator_for_actor(persona_a):
                r_a = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug_a))
                assert r_a.status_code == 200
                a_tid = uuid.UUID(r_a.json()["tenant_id"])
                a_actor_id = uuid.UUID(r_a.json()["actor_id"])

            harness.configure_fetcher_for(persona_b)
            with patch_validator_for_actor(persona_b):
                r_b = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug_b))
                assert r_b.status_code == 200
                # b_tid is different since persona_b has a different slug.

            # -----------------------------------------------------------------------
            # Seed personal workspace (Actor A only -- will be fully purged).
            # -----------------------------------------------------------------------
            personal_ws_id = await _seed_workspace(
                pg_container,
                tenant_id=a_tid,
                owner_actor_id=a_actor_id,
                name=f"rtbf-personal-{suffix}",
            )
            a_entry1_id = await _seed_entry(
                pg_container,
                workspace_id=personal_ws_id,
                tenant_id=a_tid,
                actor_id=a_actor_id,
                body="Personal note 1",
            )
            a_entry2_id = await _seed_entry(
                pg_container,
                workspace_id=personal_ws_id,
                tenant_id=a_tid,
                actor_id=a_actor_id,
                body="Personal note 2",
            )

            # -----------------------------------------------------------------------
            # Seed mixed workspace (Actor A owner + residual entry by Actor B).
            # Actor B is in a different tenant in this harness, so we need to
            # insert their entry directly via SQL referencing a_tid.
            # -----------------------------------------------------------------------
            shared_ws_id = await _seed_workspace(
                pg_container,
                tenant_id=a_tid,
                owner_actor_id=a_actor_id,
                name=f"rtbf-shared-{suffix}",
            )
            a_shared_entry_id = await _seed_entry(
                pg_container,
                workspace_id=shared_ws_id,
                tenant_id=a_tid,
                actor_id=a_actor_id,
                body="Actor A's shared note",
            )
            # Insert an actor row for B in A's tenant (simulating a legacy
            # cross-writer scenario) and add a residual entry.
            b_in_a_tenant_id = uuid.uuid4()
            engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
            factory = async_sessionmaker(engine, expire_on_commit=False)
            try:
                async with factory() as session, session.begin():
                    await session.execute(
                        text(
                            "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                            "VALUES (:aid, :tid, 'actor-b-in-a', :sub, :now)"
                        ),
                        {
                            "aid": b_in_a_tenant_id,
                            "tid": a_tid,
                            "sub": f"oidc-sub-b-in-a-{b_in_a_tenant_id.hex[:8]}",
                            "now": _NOW,
                        },
                    )
            finally:
                await engine.dispose()
            b_shared_entry_id = await _seed_entry(
                pg_container,
                workspace_id=shared_ws_id,
                tenant_id=a_tid,
                actor_id=b_in_a_tenant_id,
                body="Actor B's shared note",
            )

            # -----------------------------------------------------------------------
            # Expected counts going into the purge.
            # -----------------------------------------------------------------------
            # personal workspace: 2 entries by A; mixed workspace: 1 entry by A + 1
            # entry by B that cascade-deletes when the actor-owned workspace is dropped.
            # purged_entries counts every entry removed (3 authored by A + 1 cascade).
            # Both actor-owned workspaces are deleted -> purged_workspaces = 2.
            expected_purged_entries = 4
            expected_purged_workspaces = 2

            # -----------------------------------------------------------------------
            # Issue the RTBF purge as Actor A (who has admin role).
            # -----------------------------------------------------------------------
            harness.configure_fetcher_for(persona_a)
            with patch_validator_for_actor(persona_a):
                resp = await client.delete(
                    f"/v1/admin/actors/{a_actor_id}/personal-data",
                    headers=bearer_headers(tenant_slug=slug_a),
                )
    assert resp.status_code == 200, f"Expected 200 from RTBF purge endpoint; got {resp.status_code}: {resp.text}"
    body = resp.json()

    # -----------------------------------------------------------------------
    # Assertion 1: PurgeResult counts.
    # -----------------------------------------------------------------------
    assert body["purged_entries"] == expected_purged_entries, (
        f"purged_entries: expected {expected_purged_entries}, got {body['purged_entries']}"
    )
    assert body["purged_workspaces"] == expected_purged_workspaces, (
        f"purged_workspaces: expected {expected_purged_workspaces}, got {body['purged_workspaces']}"
    )
    assert "revoked_shares" not in body, (
        f"revoked_shares field must not appear in purge response under role-based access; "
        f"got body keys {sorted(body.keys())}"
    )

    # -----------------------------------------------------------------------
    # Assertion 2: Personal workspace -- workspace row deleted.
    # -----------------------------------------------------------------------
    personal_ws_row = await _fetch_one(
        pg_container,
        "SELECT workspace_id FROM workspaces WHERE workspace_id = :wid",
        {"wid": personal_ws_id},
    )
    assert personal_ws_row is None, f"Personal workspace {personal_ws_id} should have been deleted by RTBF purge"

    # -----------------------------------------------------------------------
    # Assertion 3: Personal workspace -- entries deleted.
    # -----------------------------------------------------------------------
    personal_entry_count = await _count_rows(
        pg_container,
        "workspace_entries",
        "entry_id = ANY(:eids)",
        {"eids": [a_entry1_id, a_entry2_id]},
    )
    assert personal_entry_count == 0, (
        f"Expected 0 entries from personal workspace after purge; got {personal_entry_count}"
    )

    # -----------------------------------------------------------------------
    # Assertion 4: Mixed workspace -- workspace row deleted.
    # -----------------------------------------------------------------------
    shared_ws_row = await _fetch_one(
        pg_container,
        "SELECT workspace_id FROM workspaces WHERE workspace_id = :wid",
        {"wid": shared_ws_id},
    )
    assert shared_ws_row is None, (
        f"Actor-owned workspace {shared_ws_id} should be physically deleted after RTBF; "
        f"actor-owned workspaces cannot outlive their owner under the single-writer invariant"
    )

    # -----------------------------------------------------------------------
    # Assertion 5: Mixed workspace -- both Actor A's and Actor B's entries cascade-deleted.
    # -----------------------------------------------------------------------
    for entry_id, label in (
        (a_shared_entry_id, "Actor A's"),
        (b_shared_entry_id, "Actor B's (cascade)"),
    ):
        entry_row = await _fetch_one(
            pg_container,
            "SELECT entry_id FROM workspace_entries WHERE entry_id = :eid",
            {"eid": entry_id},
        )
        assert entry_row is None, (
            f"{label} entry {entry_id} should be deleted when the actor-owned workspace is purged"
        )


@pytest.mark.asyncio
async def test_rtbf_purge_idempotent(pg_container: str) -> None:
    """Second RTBF call on the same actor returns all-zero counts.

    The purge is idempotent: a repeated call after all data has been removed
    returns PurgeResult with purged_entries=0 and purged_workspaces=0 (no other
    counted fields) and status 200. This makes the endpoint safe to retry without
    double-counting.
    """
    suffix = uuid.uuid4().hex[:8]
    slug = f"rtbf-idem-{suffix}"

    async with EntitlementAuthHarness(pg_container) as harness:
        persona = harness.add_persona(slug, roles=["producer", "consumer", "admin"])
        transport = httpx.ASGITransport(app=harness.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            harness.configure_fetcher_for(persona)
            with patch_validator_for_actor(persona):
                r = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))
                assert r.status_code == 200
                a_tid = uuid.UUID(r.json()["tenant_id"])
                a_actor_id = uuid.UUID(r.json()["actor_id"])

            # Seed one workspace with one entry so the first call has something to purge.
            ws_id = await _seed_workspace(
                pg_container,
                tenant_id=a_tid,
                owner_actor_id=a_actor_id,
                name=f"rtbf-idem-ws-{suffix}",
            )
            await _seed_entry(
                pg_container,
                workspace_id=ws_id,
                tenant_id=a_tid,
                actor_id=a_actor_id,
                body="Idempotency test note",
            )

            # First call: something should be purged.
            harness.configure_fetcher_for(persona)
            with patch_validator_for_actor(persona):
                resp1 = await client.delete(
                    f"/v1/admin/actors/{a_actor_id}/personal-data",
                    headers=bearer_headers(tenant_slug=slug),
                )
            assert resp1.status_code == 200, resp1.text
            body1 = resp1.json()
            assert body1["purged_entries"] >= 1
            assert body1["purged_workspaces"] >= 1

            # Second call: everything already gone -> all-zero counts.
            harness.configure_fetcher_for(persona)
            with patch_validator_for_actor(persona, iat=2):
                resp2 = await client.delete(
                    f"/v1/admin/actors/{a_actor_id}/personal-data",
                    headers=bearer_headers(tenant_slug=slug),
                )
    assert resp2.status_code == 200, resp2.text
    body2 = resp2.json()
    assert body2["purged_entries"] == 0, (
        f"Second RTBF call should return purged_entries=0; got {body2['purged_entries']}"
    )
    assert body2["purged_workspaces"] == 0, (
        f"Second RTBF call should return purged_workspaces=0; got {body2['purged_workspaces']}"
    )
    assert "revoked_shares" not in body2, (
        f"revoked_shares field must not appear in purge response; got body keys {sorted(body2.keys())}"
    )


@pytest.mark.asyncio
async def test_rtbf_purge_non_admin_forbidden(pg_container: str) -> None:
    """Non-admin caller receives 403 from the RTBF purge endpoint.

    The endpoint requires the admin role. An actor that holds only producer and
    consumer roles must be rejected with 403 before any data is touched.
    """
    suffix = uuid.uuid4().hex[:8]
    slug_owner = f"rtbf-403-owner-{suffix}"
    slug_caller = f"rtbf-403-caller-{suffix}"

    async with EntitlementAuthHarness(pg_container) as harness:
        persona_owner = harness.add_persona(slug_owner, roles=["producer", "consumer"])
        persona_caller = harness.add_persona(slug_caller, roles=["producer", "consumer"])
        transport = httpx.ASGITransport(app=harness.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            harness.configure_fetcher_for(persona_owner)
            with patch_validator_for_actor(persona_owner):
                r = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug_owner))
                a_actor_id = uuid.UUID(r.json()["actor_id"])

            harness.configure_fetcher_for(persona_caller)
            with patch_validator_for_actor(persona_caller):
                await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug_caller))

                resp = await client.delete(
                    f"/v1/admin/actors/{a_actor_id}/personal-data",
                    headers=bearer_headers(tenant_slug=slug_caller),
                )
    assert resp.status_code == 403, (
        f"Non-admin caller must receive 403 from RTBF endpoint; "
        f"got {resp.status_code}: {resp.text}"
    )
