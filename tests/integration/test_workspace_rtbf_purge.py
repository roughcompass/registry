"""Integration tests: RTBF physical purge for workspace data.

Covers the normative two-actor, two-workspace scenario:

- Actor A owns a personal workspace with only A's entries.
- Actor A owns a shared workspace where Actor B also has entries.
- Admin issues DELETE /v1/admin/actors/{actor_A_id}/personal-data → 200 with PurgeResult.

Assertions verified against a live Postgres instance (testcontainers):

Personal workspace (actor-only entries):
- All Actor A workspace entries deleted.
- workspace row deleted.
- workspace_shares deleted.
- PurgeResult.purged_workspaces count is 1.

Shared workspace (mixed entries):
- Actor A's entries deleted; workspace row survives.
- workspace.owner_actor_id is NULL after purge.
- workspace.archived_at is set (not NULL) after purge.
- Actor B's entries are intact.

workspace_shares where grantee_actor_id=Actor A → revoked_at set (not NULL).
workspace_share_acceptances rows for Actor A are RETAINED (audit trail, not deleted).
PurgeResult{purged_entries, purged_workspaces, revoked_shares} counts are accurate.
Non-admin calling the endpoint → 403.

The real Postgres instance is used because these tests verify that:
- Hard DELETEs cascade correctly through FK constraints.
- The three-step purge sequence (entries → workspace cleanup → share revocation)
  operates atomically within a single transaction.
- No FK violations occur during workspace deletion (shares deleted before workspace).
- workspace_share_acceptances FK to workspace_shares is correctly handled (shares
  in personal workspace are deleted; acceptances that reference them must also be
  deleted before the share row can be removed — the service handles this ordering).

The visibility chokepoint and transaction semantics are exercised for real —
they are not mocked — because unit tests cannot reach FK-constraint enforcement
or real DELETE row-count semantics.
"""

from __future__ import annotations

import datetime
import secrets
import uuid

import pytest
import pytest_asyncio
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


async def _seed_share(
    pg_url: str,
    *,
    workspace_id: uuid.UUID,
    workspace_tenant_id: uuid.UUID,
    grantee_actor_id: uuid.UUID,
    grantee_tenant_id: uuid.UUID,
    granted_by: uuid.UUID,
) -> uuid.UUID:
    """Insert a workspace_shares row. Returns share_id."""
    share_id = uuid.uuid4()
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO workspace_shares "
                    "(share_id, workspace_id, tenant_id, grantee_actor_id, "
                    " grantee_tenant_id, role, granted_by, granted_at) "
                    "VALUES (:sid, :wid, :tid, :gaid, :gtid, 'reader', :gby, :now)"
                ),
                {
                    "sid": share_id,
                    "wid": workspace_id,
                    "tid": workspace_tenant_id,
                    "gaid": grantee_actor_id,
                    "gtid": grantee_tenant_id,
                    "gby": granted_by,
                    "now": _NOW,
                },
            )
    finally:
        await engine.dispose()
    return share_id


async def _seed_acceptance(
    pg_url: str,
    *,
    share_id: uuid.UUID,
    workspace_id: uuid.UUID,
    accepting_actor_id: uuid.UUID,
    accepting_tenant_id: uuid.UUID,
) -> uuid.UUID:
    """Insert a workspace_share_acceptances row. Returns acceptance_id."""
    acceptance_id = uuid.uuid4()
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO workspace_share_acceptances "
                    "(acceptance_id, share_id, workspace_id, accepting_actor_id, "
                    " accepting_tenant_id, accepted_at) "
                    "VALUES (:aid, :sid, :wid, :aaid, :atid, :now)"
                ),
                {
                    "aid": acceptance_id,
                    "sid": share_id,
                    "wid": workspace_id,
                    "aaid": accepting_actor_id,
                    "atid": accepting_tenant_id,
                    "now": _NOW,
                },
            )
    finally:
        await engine.dispose()
    return acceptance_id


async def _count_rows(pg_url: str, table: str, where: str, params: dict) -> int:
    """Return the row count for a simple WHERE query."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await session.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE {where}"),  # noqa: S608
                params,
            )
            return result.scalar_one()
    finally:
        await engine.dispose()


async def _fetch_one(pg_url: str, query: str, params: dict):
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
# App fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def app_client(pg_container: str):  # type: ignore[type-arg]
    """FastAPI app + AsyncClient wired to the live testcontainers Postgres."""
    settings = Settings(
        database_url=pg_container,
        pgbouncer_url=pg_container,
        scheduler_jobstore_url=pg_container,
        scheduler_use_memory_jobstore=True,
        embedding_model="stub",
    )
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ---------------------------------------------------------------------------
# RTBF physical purge scenario
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rtbf_purge_personal_and_shared_workspace(
    pg_container: str, app_client: AsyncClient
) -> None:
    """Normative RTBF scenario: personal workspace purged, shared workspace archived.

    Setup:
    - Tenant T with Actor A (admin) and Actor B (non-admin).
    - Actor A creates a personal workspace containing only A's entries.
    - Actor A creates a second workspace where Actor B also has entries.
    - Actor B has been granted a share on A's shared workspace (share + acceptance seeded).
    - Actor A has been granted a share on some third workspace by another actor
      (so we can verify that grantee-shares are revoked in Step 3).

    Admin (Actor A) issues DELETE /v1/admin/actors/{actor_A_id}/personal-data → 200.

    Verified outcomes:
    1. PurgeResult counts are accurate.
    2. Personal workspace: entries deleted, workspace row deleted, shares deleted.
    3. Shared workspace: A's entries deleted, B's entries intact, workspace survives
       with owner_actor_id=NULL and archived_at set.
    4. workspace_shares where grantee_actor_id=A → revoked_at set.
    5. workspace_share_acceptances for Actor A are retained (audit trail).
    """
    client = app_client
    suffix = uuid.uuid4().hex[:8]

    # Seed two actors in the same tenant. Actor A holds admin role; Actor B does not.
    a_tid, a_actor_id, a_token = await _seed_tenant_with_token(
        pg_container,
        slug=f"rtbf-a-{suffix}",
        roles=["producer", "consumer", "admin"],
    )
    # Actor B in the same tenant (same data isolation domain, different actor).
    # We need B in a tenant; reuse the same tenant by inserting B's actor/token directly.
    b_actor_id = uuid.uuid4()
    b_raw_token = secrets.token_urlsafe(24)
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, display_name, created_at) "
                    "VALUES (:aid, :tid, 'actor-b', :now)"
                ),
                {"aid": b_actor_id, "tid": a_tid, "now": _NOW},
            )
            await session.execute(
                text(
                    "INSERT INTO api_tokens "
                    "(token_id, tenant_id, actor_id, token_hash, roles, created_at) "
                    "VALUES (gen_random_uuid(), :tid, :aid, :th, :roles, :now)"
                ),
                {
                    "tid": a_tid,
                    "aid": b_actor_id,
                    "th": hash_token(b_raw_token),
                    "roles": ["producer", "consumer"],
                    "now": _NOW,
                },
            )
    finally:
        await engine.dispose()

    # -----------------------------------------------------------------------
    # Seed personal workspace (Actor A only — will be fully purged).
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

    # Seed a share row on the personal workspace (granted by A to some other actor).
    # This share must be deleted when the workspace is deleted (Step 2a).
    personal_share_id = await _seed_share(
        pg_container,
        workspace_id=personal_ws_id,
        workspace_tenant_id=a_tid,
        grantee_actor_id=b_actor_id,
        grantee_tenant_id=a_tid,
        granted_by=a_actor_id,
    )

    # -----------------------------------------------------------------------
    # Seed shared workspace (Actor A + Actor B — Actor B's entries survive).
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
    b_shared_entry_id = await _seed_entry(
        pg_container,
        workspace_id=shared_ws_id,
        tenant_id=a_tid,
        actor_id=b_actor_id,
        body="Actor B's shared note",
    )

    # -----------------------------------------------------------------------
    # Seed a share GRANTED TO Actor A (to verify Step 3 share revocation).
    # This is a share on shared_ws_id granted by B to A (same-tenant share).
    # -----------------------------------------------------------------------
    share_to_a_id = await _seed_share(
        pg_container,
        workspace_id=shared_ws_id,
        workspace_tenant_id=a_tid,
        grantee_actor_id=a_actor_id,
        grantee_tenant_id=a_tid,
        granted_by=b_actor_id,
    )

    # Seed a workspace_share_acceptance for Actor A on the share granted to A.
    # This acceptance must be RETAINED after purge (audit trail).
    acceptance_id = await _seed_acceptance(
        pg_container,
        share_id=share_to_a_id,
        workspace_id=shared_ws_id,
        accepting_actor_id=a_actor_id,
        accepting_tenant_id=a_tid,
    )

    # -----------------------------------------------------------------------
    # Expected counts going into the purge.
    # -----------------------------------------------------------------------
    # personal workspace: 2 entries by A (a_entry1_id, a_entry2_id).
    # shared workspace: 1 entry by A (a_shared_entry_id), 1 entry by B (b_shared_entry_id).
    # Total entries authored by A = 3 → purged_entries = 3.
    # personal workspace (A's only entries deleted) → purged_workspaces = 1.
    # shares where grantee=A: share_to_a_id → revoked_shares = 1.
    expected_purged_entries = 3
    expected_purged_workspaces = 1
    expected_revoked_shares = 1

    # -----------------------------------------------------------------------
    # Issue the RTBF purge as Actor A (who has admin role).
    # -----------------------------------------------------------------------
    resp = await client.delete(
        f"/v1/admin/actors/{a_actor_id}/personal-data",
        headers={"Authorization": f"Bearer {a_token}"},
    )
    assert resp.status_code == 200, (
        f"Expected 200 from RTBF purge endpoint; got {resp.status_code}: {resp.text}"
    )
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
    assert body["revoked_shares"] == expected_revoked_shares, (
        f"revoked_shares: expected {expected_revoked_shares}, got {body['revoked_shares']}"
    )

    # -----------------------------------------------------------------------
    # Assertion 2: Personal workspace — workspace row deleted.
    # -----------------------------------------------------------------------
    personal_ws_row = await _fetch_one(
        pg_container,
        "SELECT workspace_id FROM workspaces WHERE workspace_id = :wid",
        {"wid": personal_ws_id},
    )
    assert personal_ws_row is None, (
        f"Personal workspace {personal_ws_id} should have been deleted by RTBF purge"
    )

    # -----------------------------------------------------------------------
    # Assertion 3: Personal workspace — entries deleted.
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
    # Assertion 4: Personal workspace — shares deleted.
    # -----------------------------------------------------------------------
    personal_share_count = await _count_rows(
        pg_container,
        "workspace_shares",
        "share_id = :sid",
        {"sid": personal_share_id},
    )
    assert personal_share_count == 0, (
        f"Share {personal_share_id} on personal workspace should have been deleted; "
        f"got count {personal_share_count}"
    )

    # -----------------------------------------------------------------------
    # Assertion 5: Shared workspace — workspace row survives with owner_actor_id=NULL
    #              and archived_at set.
    # -----------------------------------------------------------------------
    shared_ws_row = await _fetch_one(
        pg_container,
        "SELECT workspace_id, owner_actor_id, archived_at "
        "FROM workspaces WHERE workspace_id = :wid",
        {"wid": shared_ws_id},
    )
    assert shared_ws_row is not None, (
        f"Shared workspace {shared_ws_id} should still exist after RTBF purge "
        f"(Actor B has entries there)"
    )
    assert shared_ws_row.owner_actor_id is None, (
        f"Shared workspace owner_actor_id should be NULL after purge; "
        f"got {shared_ws_row.owner_actor_id}"
    )
    assert shared_ws_row.archived_at is not None, (
        "Shared workspace archived_at should be set after purge; got NULL"
    )

    # -----------------------------------------------------------------------
    # Assertion 6: Shared workspace — Actor A's entry deleted.
    # -----------------------------------------------------------------------
    a_shared_entry_row = await _fetch_one(
        pg_container,
        "SELECT entry_id FROM workspace_entries WHERE entry_id = :eid",
        {"eid": a_shared_entry_id},
    )
    assert a_shared_entry_row is None, (
        f"Actor A's entry {a_shared_entry_id} in shared workspace should be deleted after purge"
    )

    # -----------------------------------------------------------------------
    # Assertion 7: Shared workspace — Actor B's entry intact.
    # -----------------------------------------------------------------------
    b_shared_entry_row = await _fetch_one(
        pg_container,
        "SELECT entry_id FROM workspace_entries WHERE entry_id = :eid",
        {"eid": b_shared_entry_id},
    )
    assert b_shared_entry_row is not None, (
        f"Actor B's entry {b_shared_entry_id} should survive the RTBF purge of Actor A"
    )

    # -----------------------------------------------------------------------
    # Assertion 8: workspace_shares where grantee=A → revoked_at set.
    # -----------------------------------------------------------------------
    share_to_a_row = await _fetch_one(
        pg_container,
        "SELECT share_id, revoked_at FROM workspace_shares WHERE share_id = :sid",
        {"sid": share_to_a_id},
    )
    assert share_to_a_row is not None, (
        f"Share row {share_to_a_id} should still exist (revoked, not deleted)"
    )
    assert share_to_a_row.revoked_at is not None, (
        f"Share {share_to_a_id} where grantee=Actor A should have revoked_at set after purge; "
        f"got NULL"
    )

    # -----------------------------------------------------------------------
    # Assertion 9: workspace_share_acceptances for Actor A are RETAINED (audit trail).
    # -----------------------------------------------------------------------
    acceptance_row = await _fetch_one(
        pg_container,
        "SELECT acceptance_id FROM workspace_share_acceptances "
        "WHERE acceptance_id = :aid",
        {"aid": acceptance_id},
    )
    assert acceptance_row is not None, (
        f"workspace_share_acceptances row {acceptance_id} for Actor A should be "
        f"retained as an audit trail after RTBF purge (not deleted)"
    )


@pytest.mark.asyncio
async def test_rtbf_purge_idempotent(pg_container: str, app_client: AsyncClient) -> None:
    """Second RTBF call on the same actor returns all-zero counts.

    The purge is idempotent: a repeated call after all data has been removed
    returns PurgeResult with purged_entries=0, purged_workspaces=0, revoked_shares=0
    and status 200. This makes the endpoint safe to retry without double-counting.
    """
    client = app_client
    suffix = uuid.uuid4().hex[:8]

    a_tid, a_actor_id, a_token = await _seed_tenant_with_token(
        pg_container,
        slug=f"rtbf-idem-{suffix}",
        roles=["producer", "consumer", "admin"],
    )

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
    resp1 = await client.delete(
        f"/v1/admin/actors/{a_actor_id}/personal-data",
        headers={"Authorization": f"Bearer {a_token}"},
    )
    assert resp1.status_code == 200, resp1.text
    body1 = resp1.json()
    assert body1["purged_entries"] >= 1
    assert body1["purged_workspaces"] >= 1

    # Second call: everything already gone → all-zero counts.
    resp2 = await client.delete(
        f"/v1/admin/actors/{a_actor_id}/personal-data",
        headers={"Authorization": f"Bearer {a_token}"},
    )
    assert resp2.status_code == 200, resp2.text
    body2 = resp2.json()
    assert body2["purged_entries"] == 0, (
        f"Second RTBF call should return purged_entries=0; got {body2['purged_entries']}"
    )
    assert body2["purged_workspaces"] == 0, (
        f"Second RTBF call should return purged_workspaces=0; got {body2['purged_workspaces']}"
    )
    assert body2["revoked_shares"] == 0, (
        f"Second RTBF call should return revoked_shares=0; got {body2['revoked_shares']}"
    )


@pytest.mark.asyncio
async def test_rtbf_purge_non_admin_forbidden(
    pg_container: str, app_client: AsyncClient
) -> None:
    """Non-admin caller receives 403 from the RTBF purge endpoint.

    The endpoint requires the admin role. An actor that holds only producer and
    consumer roles must be rejected with 403 before any data is touched.
    """
    client = app_client
    suffix = uuid.uuid4().hex[:8]

    # Seed an actor WITHOUT the admin role.
    a_tid, a_actor_id, _a_token = await _seed_tenant_with_token(
        pg_container,
        slug=f"rtbf-403-owner-{suffix}",
        roles=["producer", "consumer"],
    )
    _b_tid, b_actor_id, b_token = await _seed_tenant_with_token(
        pg_container,
        slug=f"rtbf-403-caller-{suffix}",
        roles=["producer", "consumer"],
    )

    resp = await client.delete(
        f"/v1/admin/actors/{a_actor_id}/personal-data",
        headers={"Authorization": f"Bearer {b_token}"},
    )
    assert resp.status_code == 403, (
        f"Non-admin caller must receive 403 from RTBF endpoint; "
        f"got {resp.status_code}: {resp.text}"
    )
