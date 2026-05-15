"""Integration tests for JIT tenant + actor materialization.

Exercises the upsert paths in registry.auth.entitlements.actor_store
against a live testcontainers Postgres instance. The four contract
scenarios from the auth ADR §5 verification list:

1. First-sighting creates a tenant row.
2. Idempotent under concurrency (two parallel first-sights for the
   same slug).
3. ``disabled_at IS NOT NULL`` blocks re-creation.
4. Actor upsert returns same actor_id on re-sight; updates display_name.

These tests run against the auth-consolidation migration applied by
the session-scoped pg_container fixture (see
``tests/integration/conftest.py``).
"""

from __future__ import annotations

import asyncio
import datetime
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.auth.entitlements.actor_store import (
    DisabledTenantError,
    upsert_entitlement_actor,
    upsert_entitlement_tenant,
)


def _engine_for(pg_url: str):
    return create_async_engine(
        pg_url, connect_args={"prepared_statement_cache_size": 0}
    )


@pytest.mark.asyncio
async def test_first_sighting_creates_tenant_row(pg_container: str) -> None:
    slug = f"first-{uuid.uuid4().hex[:8]}"
    engine = _engine_for(pg_container)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            tenant_id = await upsert_entitlement_tenant(session, slug)

        async with factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT tenant_id, slug, disabled_at FROM tenants WHERE slug = :slug"
                    ),
                    {"slug": slug},
                )
            ).first()
        assert row is not None
        assert row[0] == tenant_id
        assert row[1] == slug
        assert row[2] is None  # disabled_at NULL on fresh row
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_first_sights_yield_one_row(pg_container: str) -> None:
    """Two concurrent upserts for the same new slug should both succeed
    and resolve to a single tenant row. ON CONFLICT (slug) DO UPDATE …
    RETURNING tenant_id is the safety net."""
    slug = f"race-{uuid.uuid4().hex[:8]}"
    engine = _engine_for(pg_container)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:

        async def _attempt() -> uuid.UUID:
            async with factory() as session, session.begin():
                return await upsert_entitlement_tenant(session, slug)

        results = await asyncio.gather(_attempt(), _attempt())
        # Both succeed; both return the same tenant_id (one INSERT wins,
        # the other reads the winner via DO UPDATE RETURNING).
        assert results[0] == results[1]

        async with factory() as session:
            count = (
                await session.execute(
                    text("SELECT COUNT(*) FROM tenants WHERE slug = :slug"),
                    {"slug": slug},
                )
            ).scalar()
        assert count == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_disabled_at_blocks_recreation(pg_container: str) -> None:
    slug = f"disabled-{uuid.uuid4().hex[:8]}"
    engine = _engine_for(pg_container)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        # Pre-seed a tenant row with disabled_at set.
        disabled_ts = datetime.datetime.now(tz=datetime.UTC)
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO tenants "
                    "(tenant_id, slug, display_name, created_at, is_active, disabled_at) "
                    "VALUES (gen_random_uuid(), :slug, :slug, now(), true, :disabled)"
                ),
                {"slug": slug, "disabled": disabled_ts},
            )

        # An upsert against the disabled slug must raise — and must NOT
        # modify the row.
        async with factory() as session, session.begin():
            with pytest.raises(DisabledTenantError) as exc:
                await upsert_entitlement_tenant(session, slug)
            assert exc.value.slug == slug

        # disabled_at unchanged.
        async with factory() as session:
            row = (
                await session.execute(
                    text("SELECT disabled_at FROM tenants WHERE slug = :slug"),
                    {"slug": slug},
                )
            ).first()
        assert row is not None
        assert row[0] is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_actor_upsert_idempotent_returns_same_id(pg_container: str) -> None:
    slug = f"actor-{uuid.uuid4().hex[:8]}"
    sub = f"sub-{uuid.uuid4().hex[:8]}"
    engine = _engine_for(pg_container)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        # First-sight tenant + actor.
        async with factory() as session, session.begin():
            tenant_id = await upsert_entitlement_tenant(session, slug)
            first_actor = await upsert_entitlement_actor(
                session, tenant_id, sub, "Original Name"
            )

        # Second-sight — same (tenant, sub), different display_name.
        async with factory() as session, session.begin():
            second_actor = await upsert_entitlement_actor(
                session, tenant_id, sub, "Updated Name"
            )

        assert first_actor == second_actor

        # display_name should reflect the most recent upsert.
        async with factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT display_name FROM actors "
                        "WHERE tenant_id = :tid AND oidc_subject = :sub"
                    ),
                    {"tid": tenant_id, "sub": sub},
                )
            ).first()
        assert row is not None
        assert row[0] == "Updated Name"
    finally:
        await engine.dispose()
