"""Integration capability + semver validation tests.

Covers:

- Integration entity with **< 2** ``composes`` / ``depends_on`` edges →
  lifecycle promotion rejected with 422.
- Integration entity with ≥ 2 qualifying edges → promotion succeeds.
- ``version='latest'`` → 422 with an actionable error message.

The breaking-change advisor scenarios are in
test_breaking_change_exit.py (sibling file).
"""

from __future__ import annotations

import datetime
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.config import Settings
from registry.exceptions import ValidationError
from registry.main import create_app
from registry.types import TenantContext

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)


async def _seed_tenant(pg_url: str, slug: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert tenant + actor; return (tenant_id, actor_id).

    No API token is seeded — these tests drive the service layer directly
    via TenantContext, so no bearer-token auth is required.
    """
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
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
                    "oidc_subject, created_at) VALUES (:aid, :tid, :dn, :sub, :now)"
                ),
                {
                    "aid": actor_id,
                    "tid": tenant_id,
                    "dn": f"actor-{slug}",
                    "sub": f"test-sub-{actor_id.hex[:8]}",
                    "now": _NOW,
                },
            )
            # Seed edge_rel + entity_type vocab values used in the tests.
            for kind, value in (
                ("entity_type", "integration"),
                ("entity_type", "capability"),
                ("edge_rel", "depends_on"),
                ("edge_rel", "composes"),
            ):
                await session.execute(
                    text(
                        "INSERT INTO vocabulary_values "
                        "(tenant_id, kind, value, is_system, created_at) "
                        "VALUES (:tid, :k, :v, FALSE, :now) "
                        "ON CONFLICT DO NOTHING"
                    ),
                    {"tid": tenant_id, "k": kind, "v": value, "now": _NOW},
                )
    finally:
        await engine.dispose()
    return tenant_id, actor_id


async def _seed_entity(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    entity_type: str,
    name: str,
) -> uuid.UUID:
    eid = uuid.uuid4()
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO entities "
                    "(entity_id, tenant_id, entity_type, name, is_active, "
                    " created_at, visibility) "
                    "VALUES (:eid, :tid, :etype, :name, TRUE, :now, "
                    "        'public')"
                ),
                {
                    "eid": eid,
                    "tid": tenant_id,
                    "etype": entity_type,
                    "name": name,
                    "now": _NOW,
                },
            )
            # Seed initial lifecycle = alpha (draft state).
            await session.execute(
                text(
                    "INSERT INTO attributes "
                    "(attr_id, tenant_id, entity_id, key, value, "
                    " t_valid_from, t_valid_to, t_ingested_at, t_invalidated_at) "
                    "VALUES (gen_random_uuid(), :tid, :eid, 'lifecycle', "
                    "        CAST('\"alpha\"' AS jsonb), :now, NULL, :now, NULL)"
                ),
                {"tid": tenant_id, "eid": eid, "now": _NOW},
            )
    finally:
        await engine.dispose()
    return eid


async def _add_composes_edge(pg_url: str, tenant_id: uuid.UUID, src: uuid.UUID, dst: uuid.UUID) -> None:
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO edges "
                    "(edge_id, tenant_id, src_entity_id, rel, dst_entity_id, "
                    " properties, t_valid_from, t_ingested_at) "
                    "VALUES (gen_random_uuid(), :tid, :src, 'composes', :dst, "
                    "        NULL, :now, :now)"
                ),
                {"tid": tenant_id, "src": src, "dst": dst, "now": _NOW},
            )
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def perf_app(pg_container: str):  # type: ignore[type-arg]
    settings = Settings(
        database_url=pg_container,
        pgbouncer_url=pg_container,
        scheduler_jobstore_url=pg_container,
        scheduler_use_memory_jobstore=True,
        embedding_model="stub",
    )
    yield create_app(settings)


# ---------------------------------------------------------------------------
# Integration capability lifecycle constraints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integration_promotion_fails_with_zero_qualifying_edges(pg_container: str, perf_app) -> None:
    app = perf_app
    tid, aid = await _seed_tenant(pg_container, "exit-int-zero")
    integration_id = await _seed_entity(
        pg_container,
        tenant_id=tid,
        entity_type="integration",
        name="empty-integration",
    )
    ctx = TenantContext(tenant_id=tid, actor_id=aid, roles=["producer", "admin"])

    with pytest.raises(ValidationError) as excinfo:
        await app.state.lifecycle.promote_from_draft(ctx, integration_id)
    assert "at least 2" in str(excinfo.value)


@pytest.mark.asyncio
async def test_integration_promotion_succeeds_with_two_composes_edges(pg_container: str, perf_app) -> None:
    app = perf_app
    tid, aid = await _seed_tenant(pg_container, "exit-int-two")
    integration_id = await _seed_entity(
        pg_container,
        tenant_id=tid,
        entity_type="integration",
        name="paid-integration",
    )
    member_a = await _seed_entity(pg_container, tenant_id=tid, entity_type="capability", name="member-a")
    member_b = await _seed_entity(pg_container, tenant_id=tid, entity_type="capability", name="member-b")
    await _add_composes_edge(pg_container, tid, integration_id, member_a)
    await _add_composes_edge(pg_container, tid, integration_id, member_b)

    ctx = TenantContext(tenant_id=tid, actor_id=aid, roles=["producer", "admin"])
    await app.state.lifecycle.promote_from_draft(ctx, integration_id)


# ---------------------------------------------------------------------------
# Semver enforcement on version attribute
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_version_latest_is_rejected_with_actionable_error(pg_container: str, perf_app) -> None:
    app = perf_app
    tid, aid = await _seed_tenant(pg_container, "exit-semver")
    ctx = TenantContext(tenant_id=tid, actor_id=aid, roles=["producer", "admin"])
    cap_id = await _seed_entity(pg_container, tenant_id=tid, entity_type="capability", name="cap")

    with pytest.raises(ValidationError) as excinfo:
        await app.state.catalog.update_entity(
            ctx=ctx,
            entity_id=cap_id,
            updates={"version": "latest"},
        )
    msg = str(excinfo.value)
    assert "semver" in msg.lower()
    # Actionable error includes an example version.
    assert "2.4.1" in msg or "3.0.0-alpha.1" in msg
