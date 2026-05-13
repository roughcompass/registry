"""Integration tests: cross-tenant workspace share enforcement.

Covers the normative cross-tenant share flow:
- Tenant A owns a tenant-owned workspace.
- Admin in Tenant A grants a share to Actor B (Tenant B).
- Actor B can GET the workspace (acceptance row recorded) and read entries.
- Tenant C (no share) receives 403.
- Cross-tenant share on actor-owned workspace (Layer 2 service guard) → 422.
- Direct DB INSERT bypassing the service hits the BEFORE INSERT trigger backstop.

Latency SLO: GET /v1/workspaces/{id}/entries p95 < 200 ms at 1,000 seeded entries.
Test function is named test_list_entries_p95_latency.

The service guard message and the DB trigger message are asserted verbatim so
any accidental change to either surfacing is caught immediately.

Set SKIP_LATENCY_TESTS=1 to record timing but skip the p95 assertion, e.g. in
resource-constrained CI environments that cannot hit the SLO reliably.
"""

from __future__ import annotations

import datetime
import os
import secrets
import time
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

# Exact error message produced by WorkspaceService.grant_share Layer 2 guard.
_LAYER2_ERROR = (
    "Actor-owned workspaces may only be shared within the same tenant. "
    "To share cross-tenant, the workspace must be tenant-owned."
)

# Substring of the PL/pgSQL exception raised by trg_ws_share_cross_tenant.
_TRIGGER_ERROR_FRAGMENT = "cross-tenant share rejected"


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_tenant_with_token(
    pg_url: str,
    *,
    slug: str,
    roles: list[str] | None = None,
    is_regulated: bool = False,
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
                    "created_at, is_active, is_regulated) VALUES "
                    "(:tid, :slug, :slug, :now, TRUE, :is_reg)"
                ),
                {"tid": tenant_id, "slug": slug, "now": _NOW, "is_reg": is_regulated},
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


async def _seed_workspace_entries(
    pg_url: str,
    *,
    workspace_id: uuid.UUID,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    count: int,
) -> None:
    """Bulk-insert workspace_entries via direct SQL.

    Used by the latency test to avoid the cost of 'count' POST round-trips
    through the full HTTP stack. Each entry has a distinct created_at
    timestamp (microsecond offset) so ordering is deterministic.
    """
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    base_ts = _NOW
    try:
        async with factory() as session, session.begin():
            for i in range(count):
                entry_id = uuid.uuid4()
                ts = base_ts + datetime.timedelta(microseconds=i)
                await session.execute(
                    text(
                        """
                        INSERT INTO workspace_entries (
                            entry_id, workspace_id, tenant_id,
                            kind, body_md, reference_ids,
                            created_at, updated_at, created_by
                        ) VALUES (
                            :entry_id, :workspace_id, :tenant_id,
                            'note', :body_md, '{}',
                            :ts, :ts, :actor_id
                        )
                        """
                    ),
                    {
                        "entry_id": entry_id,
                        "workspace_id": workspace_id,
                        "tenant_id": tenant_id,
                        "body_md": f"Seeded entry {i}",
                        "ts": ts,
                        "actor_id": actor_id,
                    },
                )
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
# Cross-tenant share normative flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_tenant_share_normative_flow(pg_container: str, app_client) -> None:
    """Normative cross-tenant share flow exercised against live Postgres.

    Steps:
    1. Tenant A creates a tenant-owned workspace → 201.
    2. Admin in Tenant A grants share to Actor B (Tenant B) → 201 ShareResponse.
    3. Actor B GET /v1/workspaces/{id} → 200; acceptance row recorded.
    4. Actor B GET /v1/workspaces/{id}/entries → 200.
    5. Tenant C (no share) GET /v1/workspaces/{id} → 403.
    """
    client = app_client
    suffix = uuid.uuid4().hex[:8]

    a_tid, _a_actor, a_token = await _seed_tenant_with_token(
        pg_container, slug=f"ws-share-a-{suffix}"
    )
    b_tid, b_actor, b_token = await _seed_tenant_with_token(
        pg_container, slug=f"ws-share-b-{suffix}"
    )
    _c_tid, _c_actor, c_token = await _seed_tenant_with_token(
        pg_container, slug=f"ws-share-c-{suffix}"
    )

    # Step 1: Tenant A creates a tenant-owned workspace.
    create_resp = await client.post(
        "/v1/workspaces",
        headers={"Authorization": f"Bearer {a_token}"},
        json={"name": f"shared-ws-{suffix}", "owner_kind": "tenant"},
    )
    assert create_resp.status_code == 201, create_resp.text
    workspace_id = create_resp.json()["workspace_id"]

    # Step 2: Admin in Tenant A grants share to Actor B (Tenant B).
    share_resp = await client.post(
        f"/v1/workspaces/{workspace_id}/shares",
        headers={"Authorization": f"Bearer {a_token}"},
        json={
            "grantee_actor_id": str(b_actor),
            "grantee_tenant_id": str(b_tid),
            "role": "reader",
        },
    )
    assert share_resp.status_code == 201, share_resp.text
    share_body = share_resp.json()
    assert "share_id" in share_body
    assert share_body["workspace_id"] == workspace_id
    assert share_body["grantee_actor_id"] == str(b_actor)
    assert share_body["grantee_tenant_id"] == str(b_tid)
    share_id = share_body["share_id"]

    # Step 3: Actor B accesses the workspace — acceptance row must be recorded.
    b_get_resp = await client.get(
        f"/v1/workspaces/{workspace_id}",
        headers={"Authorization": f"Bearer {b_token}"},
    )
    assert b_get_resp.status_code == 200, b_get_resp.text
    assert b_get_resp.json()["workspace_id"] == workspace_id

    # Verify workspace_share_acceptances row was written for the cross-tenant access.
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await session.execute(
                text(
                    "SELECT acceptance_id FROM workspace_share_acceptances "
                    "WHERE share_id = :sid AND accepting_actor_id = :aid"
                ),
                {"sid": uuid.UUID(share_id), "aid": b_actor},
            )
            row = result.first()
    finally:
        await engine.dispose()
    assert row is not None, (
        f"workspace_share_acceptances must have a row for share {share_id} "
        f"and actor {b_actor} after the first cross-tenant access"
    )

    # Step 4: Actor B reads entries → 200.
    b_entries_resp = await client.get(
        f"/v1/workspaces/{workspace_id}/entries",
        headers={"Authorization": f"Bearer {b_token}"},
    )
    assert b_entries_resp.status_code == 200, b_entries_resp.text
    assert "items" in b_entries_resp.json()

    # Step 5: Tenant C (no share) → 403.
    c_resp = await client.get(
        f"/v1/workspaces/{workspace_id}",
        headers={"Authorization": f"Bearer {c_token}"},
    )
    assert c_resp.status_code == 403, (
        f"Tenant C with no share must receive 403; got {c_resp.status_code}. "
        f"Response: {c_resp.text}"
    )


# ---------------------------------------------------------------------------
# Layer 2 service guard: cross-tenant share on actor-owned workspace → 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_layer2_guard_actor_owned_cross_tenant_share(pg_container: str, app_client) -> None:
    """Layer 2 service guard rejects cross-tenant share on actor-owned workspace.

    The service raises 422 before the DB INSERT is attempted, with the exact
    message declared in WorkspaceService.grant_share.
    """
    client = app_client
    suffix = uuid.uuid4().hex[:8]

    a_tid, _a_actor, a_token = await _seed_tenant_with_token(
        pg_container, slug=f"ws-l2-a-{suffix}"
    )
    b_tid, b_actor, _b_token = await _seed_tenant_with_token(
        pg_container, slug=f"ws-l2-b-{suffix}"
    )

    # Create an actor-owned workspace under Tenant A.
    create_resp = await client.post(
        "/v1/workspaces",
        headers={"Authorization": f"Bearer {a_token}"},
        json={"name": f"actor-ws-{suffix}", "owner_kind": "actor"},
    )
    assert create_resp.status_code == 201, create_resp.text
    actor_owned_id = create_resp.json()["workspace_id"]

    # Attempt to grant a cross-tenant share (b_tid != a_tid) on actor-owned workspace.
    share_resp = await client.post(
        f"/v1/workspaces/{actor_owned_id}/shares",
        headers={"Authorization": f"Bearer {a_token}"},
        json={
            "grantee_actor_id": str(b_actor),
            "grantee_tenant_id": str(b_tid),
            "role": "reader",
        },
    )
    assert share_resp.status_code == 422, (
        f"Cross-tenant share on actor-owned workspace must return 422; "
        f"got {share_resp.status_code}. Response: {share_resp.text}"
    )
    assert _LAYER2_ERROR in share_resp.json()["errors"][0]["message"], (
        f"422 error message must contain the Layer 2 guard message. "
        f"Got: {share_resp.json()['errors'][0]['message']!r}"
    )


# ---------------------------------------------------------------------------
# Layer 1 trigger backstop: direct DB INSERT on actor-owned workspace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_layer1_trigger_backstop_actor_owned_cross_tenant_share(
    pg_container: str, app_client
) -> None:
    """DB trigger trg_ws_share_cross_tenant rejects cross-tenant share bypassing the service.

    A direct SQL INSERT into workspace_shares for an actor-owned workspace with a
    cross-tenant grantee must raise a PL/pgSQL exception containing the phrase
    'cross-tenant share rejected'. This test simulates a direct-SQL path that
    bypasses the Layer 2 service guard.
    """
    client = app_client
    suffix = uuid.uuid4().hex[:8]

    a_tid, a_actor, a_token = await _seed_tenant_with_token(
        pg_container, slug=f"ws-l1-a-{suffix}"
    )
    b_tid, b_actor, _b_token = await _seed_tenant_with_token(
        pg_container, slug=f"ws-l1-b-{suffix}"
    )

    # Create an actor-owned workspace via the API.
    create_resp = await client.post(
        "/v1/workspaces",
        headers={"Authorization": f"Bearer {a_token}"},
        json={"name": f"actor-ws-l1-{suffix}", "owner_kind": "actor"},
    )
    assert create_resp.status_code == 201, create_resp.text
    ws_id = uuid.UUID(create_resp.json()["workspace_id"])

    # Attempt a direct INSERT that bypasses the service layer.
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    trigger_fired = False
    try:
        async with factory() as session, session.begin():
            try:
                await session.execute(
                    text(
                        """
                        INSERT INTO workspace_shares (
                            workspace_id, tenant_id,
                            grantee_actor_id, grantee_tenant_id,
                            role, granted_at
                        ) VALUES (
                            :ws_id, :tenant_id,
                            :grantee_actor_id, :grantee_tenant_id,
                            'reader', now()
                        )
                        """
                    ),
                    {
                        "ws_id": ws_id,
                        "tenant_id": a_tid,
                        "grantee_actor_id": b_actor,
                        "grantee_tenant_id": b_tid,
                    },
                )
            except Exception as exc:
                err_msg = str(exc)
                assert _TRIGGER_ERROR_FRAGMENT in err_msg, (
                    f"Expected PL/pgSQL trigger to raise an exception containing "
                    f"'{_TRIGGER_ERROR_FRAGMENT}'; got: {err_msg!r}"
                )
                trigger_fired = True
                raise  # Roll back the transaction.
    except Exception:
        pass  # Expected — the trigger exception causes the transaction to abort.
    finally:
        await engine.dispose()

    assert trigger_fired, (
        "The BEFORE INSERT trigger trg_ws_share_cross_tenant must have fired "
        "and raised an exception for the direct cross-tenant share INSERT."
    )


# ---------------------------------------------------------------------------
# Latency SLO: p95 < 200 ms at 1,000 entries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_entries_p95_latency(pg_container: str, app_client) -> None:
    """GET /v1/workspaces/{id}/entries p95 latency must be below 200 ms at 1,000 entries.

    Entries are seeded via direct SQL INSERT (not via POST) to avoid the cost of
    1,000 HTTP round-trips. Ten GET requests are timed with time.perf_counter; p95
    is computed as sorted(times)[int(len(times) * 0.95)].

    Set SKIP_LATENCY_TESTS=1 to record timing but skip the assertion, allowing
    resource-constrained CI environments to validate structure without failing
    on flaky timing.
    """
    client = app_client
    suffix = uuid.uuid4().hex[:8]
    skip_assertion = os.environ.get("SKIP_LATENCY_TESTS", "").strip() == "1"

    a_tid, a_actor, a_token = await _seed_tenant_with_token(
        pg_container, slug=f"ws-lat-a-{suffix}"
    )

    # Create a tenant-owned workspace.
    create_resp = await client.post(
        "/v1/workspaces",
        headers={"Authorization": f"Bearer {a_token}"},
        json={"name": f"lat-ws-{suffix}", "owner_kind": "tenant"},
    )
    assert create_resp.status_code == 201, create_resp.text
    workspace_id = uuid.UUID(create_resp.json()["workspace_id"])

    # Bulk-seed 1,000 entries via direct SQL.
    await _seed_workspace_entries(
        pg_container,
        workspace_id=workspace_id,
        tenant_id=a_tid,
        actor_id=a_actor,
        count=1000,
    )

    # Warm-up: one un-timed request to prime connection pool and query plan cache.
    warmup = await client.get(
        f"/v1/workspaces/{workspace_id}/entries",
        headers={"Authorization": f"Bearer {a_token}"},
    )
    assert warmup.status_code == 200, warmup.text

    # Timed loop: 10 sequential requests.
    times: list[float] = []
    for _ in range(10):
        t0 = time.perf_counter()
        resp = await client.get(
            f"/v1/workspaces/{workspace_id}/entries",
            headers={"Authorization": f"Bearer {a_token}"},
        )
        elapsed = time.perf_counter() - t0
        assert resp.status_code == 200, resp.text
        times.append(elapsed)

    n = len(times)
    sorted_times = sorted(times)
    p95 = sorted_times[int(n * 0.95)]

    print(
        f"\nLatency at 1,000 workspace entries — times (ms): "
        f"{[round(t * 1000, 1) for t in sorted_times]}  p95={round(p95 * 1000, 1)} ms"
    )

    if not skip_assertion:
        assert p95 < 0.200, (
            f"GET /v1/workspaces/{{id}}/entries p95 latency at 1,000 entries "
            f"is {round(p95 * 1000, 1)} ms, exceeding the 200 ms SLO. "
            f"All times (ms): {[round(t * 1000, 1) for t in sorted_times]}"
        )
