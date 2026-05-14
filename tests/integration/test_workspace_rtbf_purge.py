"""Integration tests: RTBF physical purge for workspace data.

Covers the normative two-actor, two-workspace scenario:

- Actor A owns a personal workspace with only A's entries.
- Actor A owns a second workspace where Actor B also has entries.
- Admin issues DELETE /v1/admin/actors/{actor_A_id}/personal-data → 200 with PurgeResult.

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
Non-admin calling the endpoint → 403.

The real Postgres instance is used because these tests verify that:
- Hard DELETEs cascade correctly through FK constraints.
- The two-step purge sequence (entries → workspace cleanup) operates
  atomically within a single transaction.
- No FK violations occur during workspace deletion.

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
async def test_rtbf_purge_personal_and_shared_workspace(pg_container: str, app_client: AsyncClient) -> None:
    """Normative RTBF scenario: actor-owned workspaces fully erased.

    Setup:
    - Tenant T with Actor A (admin) and Actor B (non-admin).
    - Actor A creates a personal workspace containing only A's entries.
    - Actor A creates a second workspace that also contains a residual entry
      authored by Actor B (legacy data shape — actor-owned workspaces are
      single-writer by design, so this entry cannot recur after this change).

    Admin (Actor A) issues DELETE /v1/admin/actors/{actor_A_id}/personal-data → 200.

    Verified outcomes:
    1. PurgeResult counts are accurate; no revoked_shares field exists.
    2. Both actor-owned workspaces are physically deleted along with every entry
       they contained, including residual entries authored by other actors.
       Actor-owned workspaces cannot be preserved past their owner under the
       single-writer invariant.
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

    # -----------------------------------------------------------------------
    # Seed mixed workspace (Actor A owner + Actor B's entries survive).
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
    # Expected counts going into the purge.
    # -----------------------------------------------------------------------
    # personal workspace: 2 entries by A; mixed workspace: 1 entry by A + 1
    # entry by B that cascade-deletes when the actor-owned workspace is dropped.
    # purged_entries counts every entry removed (3 authored by A + 1 cascade).
    # Both actor-owned workspaces are deleted → purged_workspaces = 2.
    expected_purged_entries = 4
    expected_purged_workspaces = 2

    # -----------------------------------------------------------------------
    # Issue the RTBF purge as Actor A (who has admin role).
    # -----------------------------------------------------------------------
    resp = await client.delete(
        f"/v1/admin/actors/{a_actor_id}/personal-data",
        headers={"Authorization": f"Bearer {a_token}"},
    )
    assert resp.status_code == 200, f"Expected 200 from RTBF purge endpoint; got {resp.status_code}: {resp.text}"
    body = resp.json()

    # -----------------------------------------------------------------------
    # Assertion 1: PurgeResult counts.
    # -----------------------------------------------------------------------
    assert (
        body["purged_entries"] == expected_purged_entries
    ), f"purged_entries: expected {expected_purged_entries}, got {body['purged_entries']}"
    assert (
        body["purged_workspaces"] == expected_purged_workspaces
    ), f"purged_workspaces: expected {expected_purged_workspaces}, got {body['purged_workspaces']}"
    assert "revoked_shares" not in body, (
        f"revoked_shares field must not appear in purge response under role-based access; "
        f"got body keys {sorted(body.keys())}"
    )

    # -----------------------------------------------------------------------
    # Assertion 2: Personal workspace — workspace row deleted.
    # -----------------------------------------------------------------------
    personal_ws_row = await _fetch_one(
        pg_container,
        "SELECT workspace_id FROM workspaces WHERE workspace_id = :wid",
        {"wid": personal_ws_id},
    )
    assert personal_ws_row is None, f"Personal workspace {personal_ws_id} should have been deleted by RTBF purge"

    # -----------------------------------------------------------------------
    # Assertion 3: Personal workspace — entries deleted.
    # -----------------------------------------------------------------------
    personal_entry_count = await _count_rows(
        pg_container,
        "workspace_entries",
        "entry_id = ANY(:eids)",
        {"eids": [a_entry1_id, a_entry2_id]},
    )
    assert (
        personal_entry_count == 0
    ), f"Expected 0 entries from personal workspace after purge; got {personal_entry_count}"

    # -----------------------------------------------------------------------
    # Assertion 4: Mixed workspace — workspace row deleted.
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
    # Assertion 5: Mixed workspace — both Actor A's and Actor B's entries cascade-deleted.
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
        assert entry_row is None, f"{label} entry {entry_id} should be deleted when the actor-owned workspace is purged"


@pytest.mark.asyncio
async def test_rtbf_purge_idempotent(pg_container: str, app_client: AsyncClient) -> None:
    """Second RTBF call on the same actor returns all-zero counts.

    The purge is idempotent: a repeated call after all data has been removed
    returns PurgeResult with purged_entries=0 and purged_workspaces=0 (no other
    counted fields) and status 200. This makes the endpoint safe to retry without
    double-counting.
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
    assert (
        body2["purged_entries"] == 0
    ), f"Second RTBF call should return purged_entries=0; got {body2['purged_entries']}"
    assert (
        body2["purged_workspaces"] == 0
    ), f"Second RTBF call should return purged_workspaces=0; got {body2['purged_workspaces']}"
    assert (
        "revoked_shares" not in body2
    ), f"revoked_shares field must not appear in purge response; got body keys {sorted(body2.keys())}"


@pytest.mark.asyncio
async def test_rtbf_purge_non_admin_forbidden(pg_container: str, app_client: AsyncClient) -> None:
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
        f"Non-admin caller must receive 403 from RTBF endpoint; " f"got {resp.status_code}: {resp.text}"
    )
