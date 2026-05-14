"""Performance test — workspace list_entries p95 < 200 ms at 1,000 entries.

SLO
---
``GET /v1/workspaces/{id}/entries`` on a workspace seeded with 1,000 active
plaintext entries (``body_md NOT NULL``, ``kind='note'``,
``t_invalidated_at IS NULL``) must complete with p95 latency < 200 ms in the
testcontainers environment.

This file is the release-pipeline SLO gate. It is distinct from the p95
timing assertion in the integration test suite which runs as part of
``make test-integration``; this file is excluded from ``make test`` and
``make test-unit`` and runs only via ``make test-perf`` in the release
pipeline. Keeping the SLO gate separate means the default fast CI loop stays
unaffected by the testcontainers startup cost and the 1,000-row seeding time.

Setup
-----
- 1 tenant, 1 actor, 1 API token (roles: producer + consumer + admin).
- 1 tenant-owned workspace.
- 1,000 entries seeded via direct SQL INSERT (not via POST) to avoid 1,000
  HTTP round-trips inflating fixture cost.
- 1 warm-up GET request (un-timed) to prime the connection pool and query
  plan cache.
- 10 timed sequential GET requests; p95 is computed as
  ``sorted(times)[int(len(times) * 0.95)]``.

Marks
-----
``@pytest.mark.perf`` + ``@pytest.mark.slow`` — excluded from unit-only CI.
To run: ``pytest tests/perf/test_perf_workspace_read_tier1.py -m perf -v``.

Note: testcontainers runs a single-node Postgres on the local Docker daemon.
Production hardware with a tuned Postgres instance will see lower latency.
"""

from __future__ import annotations

import datetime
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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)
_ENTRY_COUNT = 1000
_TIMED_CALLS = 10
_P95_TARGET_S = 0.200  # 200 ms expressed in seconds (matches time.perf_counter units)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_tenant(
    pg_url: str,
    *,
    slug: str,
) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Insert tenant + actor + api_token. Returns (tenant_id, actor_id, raw_token)."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    raw_token = secrets.token_urlsafe(24)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO tenants (tenant_id, slug, display_name, "
                    "created_at, is_active, is_regulated) VALUES "
                    "(:tid, :slug, :slug, :now, TRUE, FALSE)"
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
            role_names = ["producer", "consumer", "admin"]
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
                    "roles": role_names,
                    "now": _NOW,
                },
            )
            # Workspace authorization reads actor_roles JOIN roles to derive the
            # effective role set; seed both tables so the perf actor passes the
            # role-based visibility predicate without going through the public
            # role-assignment surface.
            for role_name in role_names:
                role_id = uuid.uuid4()
                await session.execute(
                    text(
                        "INSERT INTO roles "
                        "(role_id, tenant_id, name, permissions, created_at) "
                        "VALUES (:rid, :tid, :name, '{}', :now) "
                        "ON CONFLICT DO NOTHING"
                    ),
                    {"rid": role_id, "tid": tenant_id, "name": role_name, "now": _NOW},
                )
                await session.execute(
                    text(
                        "INSERT INTO actor_roles "
                        "(actor_id, role_id, tenant_id, granted_at) "
                        "SELECT :aid, r.role_id, :tid, :now "
                        "FROM roles r "
                        "WHERE r.tenant_id = :tid AND r.name = :name "
                        "ON CONFLICT DO NOTHING"
                    ),
                    {"aid": actor_id, "tid": tenant_id, "name": role_name, "now": _NOW},
                )
    finally:
        await engine.dispose()
    return tenant_id, actor_id, raw_token


async def _seed_entries(
    pg_url: str,
    *,
    workspace_id: uuid.UUID,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    count: int,
) -> None:
    """Bulk-insert ``count`` active plaintext entries via direct SQL.

    Each entry receives a distinct ``created_at`` microsecond offset so
    ordering is deterministic. All entries are kind='note', body_md NOT NULL,
    and t_invalidated_at IS NULL (active).
    """
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    base_ts = _NOW
    batch_size = 200
    try:
        for batch_start in range(0, count, batch_size):
            batch_end = min(batch_start + batch_size, count)
            async with factory() as session, session.begin():
                for i in range(batch_start, batch_end):
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
                            "entry_id": uuid.uuid4(),
                            "workspace_id": workspace_id,
                            "tenant_id": tenant_id,
                            "body_md": f"Perf seed entry {i}",
                            "ts": ts,
                            "actor_id": actor_id,
                        },
                    )
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Module-level fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def perf_workspace_setup(pg_container: str):  # type: ignore[type-arg]
    """Seed tenant, workspace, and 1,000 entries for the test."""
    suffix = uuid.uuid4().hex[:8]

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
        # Seed tenant + actor + token via direct SQL (fast).
        tenant_id, actor_id, raw_token = await _seed_tenant(
            pg_container,
            slug=f"perf-ws-t1-{suffix}",
        )
        headers = {"Authorization": f"Bearer {raw_token}"}

        # Create workspace via HTTP (validates auth + ownership logic correctly).
        create_resp = await client.post(
            "/v1/workspaces",
            headers=headers,
            json={"name": f"perf-ws-{suffix}", "owner_kind": "tenant"},
        )
        assert create_resp.status_code == 201, f"Workspace creation failed: {create_resp.text}"
        workspace_id = uuid.UUID(create_resp.json()["workspace_id"])

        # Bulk-seed 1,000 entries via direct SQL to keep fixture cost low.
        await _seed_entries(
            pg_container,
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            count=_ENTRY_COUNT,
        )

        yield {
            "client": client,
            "workspace_id": workspace_id,
            "token": raw_token,
        }


# ---------------------------------------------------------------------------
# Performance test
# ---------------------------------------------------------------------------


@pytest.mark.perf
@pytest.mark.slow
@pytest.mark.asyncio
async def test_list_entries_p95_tier1(perf_workspace_setup: dict) -> None:
    """GET /v1/workspaces/{id}/entries p95 must be < 200 ms at 1,000 active entries.

    Methodology:
    - 1 un-timed warm-up request to prime the connection pool and query plan cache.
    - 10 timed sequential GET requests recorded via time.perf_counter.
    - p95 computed as sorted(times)[int(len(times) * 0.95)].
    - Threshold: p95 < 200 ms. Measured p95 is printed on failure to aid diagnosis.
    """
    client: AsyncClient = perf_workspace_setup["client"]
    workspace_id: uuid.UUID = perf_workspace_setup["workspace_id"]
    token: str = perf_workspace_setup["token"]
    headers = {"Authorization": f"Bearer {token}"}
    url = f"/v1/workspaces/{workspace_id}/entries"

    # Warm-up: un-timed to stabilise connection pool and query plan cache.
    warmup_resp = await client.get(url, headers=headers)
    assert warmup_resp.status_code == 200, f"Warm-up request failed: {warmup_resp.text}"

    # Timed loop.
    times: list[float] = []
    for _ in range(_TIMED_CALLS):
        t0 = time.perf_counter()
        resp = await client.get(url, headers=headers)
        elapsed = time.perf_counter() - t0
        assert resp.status_code == 200, f"Timed request failed: {resp.text}"
        times.append(elapsed)

    n = len(times)
    sorted_times = sorted(times)
    p95 = sorted_times[int(n * 0.95)]

    print(
        f"\nWorkspace list_entries latency at {_ENTRY_COUNT} entries ({n} calls): "
        f"times (ms)={[round(t * 1000, 1) for t in sorted_times]}  "
        f"p95={round(p95 * 1000, 1)} ms"
    )

    assert p95 < _P95_TARGET_S, (
        f"GET /v1/workspaces/{{id}}/entries p95 at {_ENTRY_COUNT} entries "
        f"is {round(p95 * 1000, 1)} ms, exceeding the {_P95_TARGET_S * 1000:.0f} ms SLO. "
        f"All times (ms): {[round(t * 1000, 1) for t in sorted_times]}"
    )
