"""Integration tests: JIT tenant and actor materialization via the RSAM upsert path.

Exercises the DB-layer semantics of `upsert_rsam_tenant` and `upsert_rsam_actor`
against a live Postgres instance (testcontainers). No HTTP layer is involved —
these scenarios target the single-transaction atomicity, concurrent first-sight
idempotency, and audit-row emission that must hold regardless of the HTTP surface.

Scenarios covered:
1. First-sight SEAL creates a tenants row with provider='jit' and the correct
   external_tenant_id.
2. First-sight also creates an actor row scoped to (tenant_id, oidc_subject).
3. Concurrent first-sight of the same SEAL via asyncio.gather results in exactly
   one tenants row and one actors row — the ON CONFLICT DO NOTHING path absorbs
   the duplicate.
4. Both audit events (tenant.jit_created and actor.jit_created) carry
   source='rsam' in the payload and are written atomically with their parent rows.
5. Re-sight of the same SEAL leaves row counts unchanged and does not emit
   duplicate audit events.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.auth.rsam.tenant_store import upsert_rsam_actor, upsert_rsam_tenant

# ---------------------------------------------------------------------------
# Helpers — direct DB queries for assertions


async def _count_tenant_rows(session: AsyncSession, seal_id: str) -> int:
    result = await session.execute(
        text("SELECT COUNT(*) FROM tenants " "WHERE external_tenant_id = :seal AND provider = 'jit'"),
        {"seal": seal_id},
    )
    row = result.fetchone()
    return int(row[0]) if row else 0


async def _fetch_tenant_row(session: AsyncSession, seal_id: str) -> dict[str, Any] | None:
    result = await session.execute(
        text(
            "SELECT tenant_id, slug, display_name, is_active, external_tenant_id, provider "
            "FROM tenants WHERE external_tenant_id = :seal AND provider = 'jit'"
        ),
        {"seal": seal_id},
    )
    row = result.fetchone()
    if row is None:
        return None
    cols = ["tenant_id", "slug", "display_name", "is_active", "external_tenant_id", "provider"]
    return dict(zip(cols, row, strict=False))


async def _count_actor_rows(session: AsyncSession, tenant_id: uuid.UUID, oidc_subject: str) -> int:
    result = await session.execute(
        text("SELECT COUNT(*) FROM actors " "WHERE tenant_id = :tid AND oidc_subject = :sub"),
        {"tid": tenant_id, "sub": oidc_subject},
    )
    row = result.fetchone()
    return int(row[0]) if row else 0


async def _fetch_audit_rows(
    session: AsyncSession,
    action: str,
    tenant_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """Fetch audit_log rows for the given action, optionally scoped to a tenant."""
    if tenant_id is not None:
        result = await session.execute(
            text("SELECT action, after_jsonb FROM audit_log " "WHERE action = :action AND tenant_id = :tid"),
            {"action": action, "tid": tenant_id},
        )
    else:
        result = await session.execute(
            text("SELECT action, after_jsonb FROM audit_log WHERE action = :action"),
            {"action": action},
        )
    rows = result.fetchall()
    return [{"action": r[0], "after_jsonb": r[1]} for r in rows]


# ---------------------------------------------------------------------------
# Fixture: per-test async session factory pointing at the shared container


@pytest_asyncio.fixture
async def session_factory(pg_container: str):
    """Return an async_sessionmaker bound to the shared Postgres container."""
    engine = create_async_engine(
        pg_container,
        connect_args={"prepared_statement_cache_size": 0},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


# ---------------------------------------------------------------------------
# Scenario 1: first-sight SEAL creates a tenants row with provider='jit'


@pytest.mark.asyncio
async def test_first_sight_seal_creates_tenant_row(
    session_factory,
) -> None:
    """First call to upsert_rsam_tenant inserts one row with provider='jit' and
    the correct external_tenant_id, slug, display_name, and is_active=True.
    """
    seal_id = "112025"

    async with session_factory() as session, session.begin():
        tenant_id = await upsert_rsam_tenant(session, seal_id)

    assert isinstance(tenant_id, uuid.UUID)

    async with session_factory() as session:
        row = await _fetch_tenant_row(session, seal_id)

    assert row is not None, "tenants row must exist after first-sight upsert"
    assert row["provider"] == "jit"
    assert row["external_tenant_id"] == seal_id
    assert row["slug"] == seal_id
    assert row["display_name"] == f"SEAL {seal_id}"
    assert row["is_active"] is True
    assert row["tenant_id"] == tenant_id


# ---------------------------------------------------------------------------
# Scenario 2: first-sight also creates an actor row for (tenant_id, oidc_subject)


@pytest.mark.asyncio
async def test_first_sight_creates_actor_row(session_factory) -> None:
    """upsert_rsam_actor inserts one actors row with oidc_subject set."""
    seal_id = "221100"
    oidc_subject = "F731821"

    async with session_factory() as session, session.begin():
        tenant_id = await upsert_rsam_tenant(session, seal_id)

    async with session_factory() as session, session.begin():
        await upsert_rsam_actor(session, tenant_id, oidc_subject)

    async with session_factory() as session:
        count = await _count_actor_rows(session, tenant_id, oidc_subject)

    assert count == 1, "exactly one actor row must exist after first-sight actor upsert"


# ---------------------------------------------------------------------------
# Scenario 3: concurrent first-sight is idempotent


@pytest.mark.asyncio
async def test_concurrent_first_sight_idempotent(session_factory) -> None:
    """Two concurrent upsert_rsam_tenant calls for the same SEAL produce exactly
    one tenants row and one actors row (ON CONFLICT DO NOTHING absorbs the race).
    """
    seal_id = "332211"
    oidc_subject = "F999001"

    async def _provision() -> uuid.UUID:
        async with session_factory() as session, session.begin():
            tid = await upsert_rsam_tenant(session, seal_id)
            await upsert_rsam_actor(session, tid, oidc_subject)
            return tid

    tenant_ids = await asyncio.gather(_provision(), _provision())

    # Both calls must return the same tenant UUID.
    assert tenant_ids[0] == tenant_ids[1], "concurrent upserts must return the same tenant_id"

    # Exactly one tenants row must exist.
    async with session_factory() as session:
        tenant_count = await _count_tenant_rows(session, seal_id)
        actor_count = await _count_actor_rows(session, tenant_ids[0], oidc_subject)

    assert tenant_count == 1, f"expected 1 tenants row, found {tenant_count}"
    assert actor_count == 1, f"expected 1 actors row, found {actor_count}"


# ---------------------------------------------------------------------------
# Scenario 4: audit events carry source='rsam' in payload


@pytest.mark.asyncio
async def test_jit_tenant_audit_event_emitted(session_factory) -> None:
    """upsert_rsam_tenant emits a tenant.jit_created audit row in the same
    transaction, with source='rsam' in the after_jsonb payload.
    """
    seal_id = "445566"

    async with session_factory() as session, session.begin():
        tenant_id = await upsert_rsam_tenant(session, seal_id)

    async with session_factory() as session:
        rows = await _fetch_audit_rows(session, "tenant.jit_created", tenant_id)

    assert len(rows) >= 1, "expected at least one tenant.jit_created audit row"
    payload: dict = rows[0]["after_jsonb"]
    assert payload.get("source") == "rsam", f"expected source='rsam' in payload: {payload}"
    assert payload.get("provider") == "jit", f"expected provider='jit' in payload: {payload}"
    assert payload.get("external_tenant_id") == seal_id


@pytest.mark.asyncio
async def test_jit_actor_audit_event_emitted(session_factory) -> None:
    """upsert_rsam_actor emits an actor.jit_created audit row with source='rsam'."""
    seal_id = "556677"
    oidc_subject = "F111222"

    async with session_factory() as session, session.begin():
        tenant_id = await upsert_rsam_tenant(session, seal_id)

    async with session_factory() as session, session.begin():
        await upsert_rsam_actor(session, tenant_id, oidc_subject)

    async with session_factory() as session:
        rows = await _fetch_audit_rows(session, "actor.jit_created", tenant_id)

    assert len(rows) >= 1, "expected at least one actor.jit_created audit row"
    payload: dict = rows[0]["after_jsonb"]
    assert payload.get("source") == "rsam", f"expected source='rsam' in payload: {payload}"
    assert payload.get("oidc_subject") == oidc_subject


# ---------------------------------------------------------------------------
# Scenario 5: re-sight is idempotent — no new rows, no new audit events


@pytest.mark.asyncio
async def test_re_sight_is_idempotent(session_factory) -> None:
    """A second call for the same SEAL produces no new tenants row, no new actors
    row, and no duplicate audit events.
    """
    seal_id = "667788"
    oidc_subject = "F200300"

    # First sight
    async with session_factory() as session, session.begin():
        tenant_id = await upsert_rsam_tenant(session, seal_id)
    async with session_factory() as session, session.begin():
        await upsert_rsam_actor(session, tenant_id, oidc_subject)

    # Record audit counts after first sight
    async with session_factory() as session:
        tenant_audit_after_first = await _fetch_audit_rows(session, "tenant.jit_created", tenant_id)
        actor_audit_after_first = await _fetch_audit_rows(session, "actor.jit_created", tenant_id)

    tenant_audit_count_first = len(tenant_audit_after_first)
    actor_audit_count_first = len(actor_audit_after_first)

    # Second sight (re-sight)
    async with session_factory() as session, session.begin():
        tenant_id_2 = await upsert_rsam_tenant(session, seal_id)
    async with session_factory() as session, session.begin():
        await upsert_rsam_actor(session, tenant_id_2, oidc_subject)

    # Tenant UUID must be stable
    assert tenant_id_2 == tenant_id, "re-sight must return the same tenant_id"

    # Row counts must not grow
    async with session_factory() as session:
        tenant_count = await _count_tenant_rows(session, seal_id)
        actor_count = await _count_actor_rows(session, tenant_id, oidc_subject)
        tenant_audit_after_second = await _fetch_audit_rows(session, "tenant.jit_created", tenant_id)
        actor_audit_after_second = await _fetch_audit_rows(session, "actor.jit_created", tenant_id)

    assert tenant_count == 1, "re-sight must not create a second tenants row"
    assert actor_count == 1, "re-sight must not create a second actors row"
    assert (
        len(tenant_audit_after_second) == tenant_audit_count_first
    ), "re-sight must not emit a duplicate tenant.jit_created audit event"
    assert (
        len(actor_audit_after_second) == actor_audit_count_first
    ), "re-sight must not emit a duplicate actor.jit_created audit event"
