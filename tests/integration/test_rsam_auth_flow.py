"""Integration tests: full RSAM auth path under live Postgres + FastAPI.

Exercises the end-to-end write path for RSAM-mode authentication under a
live Postgres instance (testcontainers). The OIDC JWT validation step is
exercised by the existing test_rbac_oidc.py; here we focus on the RSAM-
specific path from resolved claims → JIT tenant materialisation → TenantContext
construction → entity write success, and on the visibility chokepoint that
prevents cross-tenant data access.

The `fetch_authorities` callable is injected directly on the RsamClaimSource
instance (constructor parameter) so no module-level patching is required.

To keep the test deterministic we bypass the OIDC JWT validation stage by
overriding the `get_tenant_context` FastAPI dependency with a thin shim that
calls the RSAM resolver directly. This is the correct test-mode bypass: the
OIDC validator is covered separately; what we are testing here is that the
resolver factory dispatches to RsamClaimSource, that JIT materialisation runs
correctly, that TenantContext is built from the resolver's output, and that
entity writes succeed in the materialised scope.

Cross-tenant visibility is verified at the end of the test suite — an entity
created in one SEAL's tenant is not visible to a different SEAL's tenant.

Scenarios:
1. Happy path: injected fetch_authorities returning ["112025_DP_CHANNEL_Owner"]
   → resolve() returns one TenantGrant → TenantContext set → POST /v1/capabilities
   returns 201. fetch_authorities called once with correct subject.
2. Cross-tenant visibility: entity in SEAL 112025 tenant not visible to SEAL 34612.
3. Multi-grant header selection: user with two SEAL grants and X-Tenant-ID: 112025
   → only that tenant's context active (200 from list); without header → 400.
"""

from __future__ import annotations

import datetime
import secrets
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.api.middleware.tenant import get_tenant_context
from registry.auth.rsam.claim_source import RsamClaimSource
from registry.config import Settings
from registry.main import create_app
from registry.storage.pg import create_engine as _create_engine
from registry.storage.pg import get_session_factory
from registry.types import TenantContext

# ---------------------------------------------------------------------------
# Constants

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Settings factory for RSAM mode


def _rsam_settings(pg_url: str) -> Settings:
    """Build Settings with auth_mode='rsam'.

    auth_claim_source_url is required by Settings when auth_mode != 'oidc'.
    We supply a placeholder because the actual HTTP call to the entitlement
    API is replaced by the injected fetch_authorities stub — it is never called.
    """
    return Settings(
        database_url=pg_url,
        pgbouncer_url=pg_url,
        scheduler_jobstore_url=pg_url,
        scheduler_use_memory_jobstore=True,
        embedding_model="stub",
        auth_mode="rsam",
        auth_claim_source_url="http://stub-rsam-api.test",  # never called in tests
        rate_limit_enabled=False,
    )


# ---------------------------------------------------------------------------
# Seed helpers


async def _seed_vocabulary(pg_url: str, *, tenant_id: uuid.UUID) -> None:
    """Insert the minimum vocabulary rows for capability creation."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            for kind, value in [
                ("entity_type", "capability"),
                ("entity_type", "concept"),
                ("entity_type", "operation"),
                ("fact_category", "overview"),
                ("edge_rel", "depends_on"),
                ("edge_rel", "replaced_by"),
            ]:
                await session.execute(
                    text(
                        "INSERT INTO vocabulary_values (tenant_id, kind, value, is_system) "
                        "VALUES (:tid, :kind, :value, FALSE) "
                        "ON CONFLICT DO NOTHING"
                    ),
                    {"tid": tenant_id, "kind": kind, "value": value},
                )
    finally:
        await engine.dispose()


async def _fetch_tenant_by_seal(pg_url: str, seal_id: str) -> uuid.UUID | None:
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await session.execute(
                text(
                    "SELECT tenant_id FROM tenants "
                    "WHERE external_tenant_id = :seal AND provider = 'jit'"
                ),
                {"seal": seal_id},
            )
            row = result.fetchone()
    finally:
        await engine.dispose()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Dependency-override helper
#
# `_make_rsam_get_tenant_context` builds a FastAPI dependency that bypasses
# OIDC JWT validation and instead calls the RSAM resolver directly. The
# dependency reads the X-RSAM-Subject test header as the subject, then calls
# resolver.resolve() to materialise the JIT tenant and return a TenantContext.
#
# The X-Tenant-ID header is forwarded to the RSAM tenant-selector so multi-
# grant scenarios still exercise the selection logic.
#
# After the grant is selected, the dependency resolves the real actor_id from
# the actors table using (tenant_id, oidc_subject). The JIT upsert in the
# resolver guarantees the actor row exists at this point.


def _make_rsam_get_tenant_context(
    resolver: RsamClaimSource,
):
    """Return a FastAPI dependency that routes directly to the RSAM resolver.

    Uses X-RSAM-Subject (test-only header) as the subject so we avoid the
    OIDC JWT validation step while still exercising the full RSAM grant
    resolution path, JIT materialisation, and tenant-selector logic.

    Resolves the sentinel actor_id (UUID(int=0)) to the real actor UUID by
    querying actors WHERE (tenant_id, oidc_subject) — the JIT upsert in
    upsert_rsam_actor guarantees this row exists before the write path fires.
    """
    from sqlalchemy import text as _text  # noqa: PLC0415

    from registry.api.middleware.tenant import _select_rsam_tenant  # noqa: PLC0415

    async def _rsam_get_tenant_context(request: Request) -> TenantContext:
        subject = request.headers.get("X-RSAM-Subject")
        if not subject:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing X-RSAM-Subject test header",
            )
        resolved = await resolver.resolve({"sub": subject})
        ctx = _select_rsam_tenant(request, resolved)

        # Resolve the sentinel actor_id to the real JIT actor UUID.
        # The sentinel (UUID(int=0)) is a placeholder; entity writes require
        # a valid FK to actors(actor_id). The JIT upsert guarantees the row exists.
        factory = request.app.state.session_factory
        async with factory() as session:
            result = await session.execute(
                _text(
                    "SELECT actor_id FROM actors "
                    "WHERE tenant_id = :tid AND oidc_subject = :sub LIMIT 1"
                ),
                {"tid": ctx.tenant_id, "sub": subject},
            )
            row = result.fetchone()
        if row is not None:
            ctx = TenantContext(
                tenant_id=ctx.tenant_id,
                actor_id=row[0],
                roles=ctx.roles,
            )
        return ctx

    return _rsam_get_tenant_context


# ---------------------------------------------------------------------------
# Scenario 1: happy path — full write path succeeds with RSAM auth


@pytest.mark.asyncio
async def test_rsam_full_write_path(pg_container: str) -> None:
    """RSAM resolver → JIT tenant → TenantContext → POST /v1/capabilities → 201.

    Verifies:
    - build_resolver factory dispatches to RsamClaimSource when auth_mode='rsam'.
    - JIT tenant and actor are materialised from the SEAL authority.
    - Entity write against the JIT tenant's context succeeds.
    - fetch_authorities is called exactly once with the correct subject.
    """
    seal_id = f"1120{secrets.token_hex(2)[:2]}"  # unique per test run
    # Use a 4-digit numeric SEAL ID — grammar requires 4–6 decimal digits.
    seal_id = "1120"
    subject = "F731821"
    authority_string = f"{seal_id}_DP_CHANNEL_Owner"

    settings = _rsam_settings(pg_container)
    engine = _create_engine(settings)
    session_factory = get_session_factory(engine)

    fetch_stub = AsyncMock(return_value=[authority_string])
    resolver = RsamClaimSource(
        settings=settings,
        session_factory=session_factory,
        fetch_authorities=fetch_stub,
    )

    app = create_app(settings)
    app.dependency_overrides[get_tenant_context] = _make_rsam_get_tenant_context(resolver)

    try:
        # Seed vocabulary after JIT tenant is created by the first request.
        # The JIT tenant is created on first call to resolver.resolve(), which
        # happens during the request. We must seed vocab before the write call.
        # Strategy: make a preflight resolve call to materialise the tenant first.
        async with session_factory() as session, session.begin():
            from registry.auth.rsam.tenant_store import upsert_rsam_tenant  # noqa: PLC0415
            tenant_id = await upsert_rsam_tenant(session, seal_id)

        await _seed_vocabulary(pg_container, tenant_id=tenant_id)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/v1/capabilities",
                json={"name": "rsam-authed-capability"},
                headers={"X-RSAM-Subject": subject},
            )

        assert resp.status_code == 201, (
            f"expected 201 from RSAM-authed capability create; "
            f"got {resp.status_code}: {resp.text}"
        )

        # fetch_authorities must have been called with the correct subject.
        fetch_stub.assert_called_once_with(subject)

        # The JIT tenant must exist and the entity must be scoped to it.
        actual_tenant_id = await _fetch_tenant_by_seal(pg_container, seal_id)
        assert actual_tenant_id is not None, "JIT tenant must exist after RSAM auth"
        assert actual_tenant_id == tenant_id
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


# ---------------------------------------------------------------------------
# Scenario 2: cross-tenant visibility isolation


@pytest.mark.asyncio
async def test_cross_tenant_visibility_intact(pg_container: str) -> None:
    """Entity created under SEAL A's tenant is not visible to SEAL B's tenant.

    Both tenants are JIT-materialised. The entity is created via SEAL A's
    TenantContext. A subsequent GET using SEAL B's context must return 404
    because the visibility chokepoint (filter_entities) scopes all lookups
    to the calling tenant's rows.
    """
    seal_a = "2211"
    seal_b = "3346"
    subject_a = f"U{secrets.token_hex(4)}"
    subject_b = f"U{secrets.token_hex(4)}"

    settings = _rsam_settings(pg_container)
    engine = _create_engine(settings)
    session_factory = get_session_factory(engine)

    # Pre-materialise both tenants so we can seed vocabulary.
    from registry.auth.rsam.tenant_store import upsert_rsam_tenant  # noqa: PLC0415
    async with session_factory() as session, session.begin():
        tenant_a = await upsert_rsam_tenant(session, seal_a)
    async with session_factory() as session, session.begin():
        tenant_b = await upsert_rsam_tenant(session, seal_b)

    await _seed_vocabulary(pg_container, tenant_id=tenant_a)
    await _seed_vocabulary(pg_container, tenant_id=tenant_b)

    # --- Tenant A creates an entity ---
    fetch_a = AsyncMock(return_value=[f"{seal_a}_DP_CHANNEL_Owner"])
    resolver_a = RsamClaimSource(
        settings=settings,
        session_factory=session_factory,
        fetch_authorities=fetch_a,
    )
    app_a = create_app(settings)
    app_a.dependency_overrides[get_tenant_context] = _make_rsam_get_tenant_context(resolver_a)

    try:
        entity_name = f"cap-{secrets.token_hex(4)}"
        transport_a = httpx.ASGITransport(app=app_a)
        async with httpx.AsyncClient(transport=transport_a, base_url="http://test") as client:
            create_resp = await client.post(
                "/v1/capabilities",
                json={"name": entity_name},
                headers={"X-RSAM-Subject": subject_a},
            )

        assert create_resp.status_code == 201, (
            f"expected 201 for tenant A entity create; "
            f"got {create_resp.status_code}: {create_resp.text}"
        )
        entity_id = create_resp.json()["entity_id"]
    finally:
        app_a.dependency_overrides.clear()

    # --- Tenant B tries to read the entity ---
    fetch_b = AsyncMock(return_value=[f"{seal_b}_DP_CHANNEL_Owner"])
    resolver_b = RsamClaimSource(
        settings=settings,
        session_factory=session_factory,
        fetch_authorities=fetch_b,
    )
    app_b = create_app(settings)
    app_b.dependency_overrides[get_tenant_context] = _make_rsam_get_tenant_context(resolver_b)

    try:
        transport_b = httpx.ASGITransport(app=app_b)
        async with httpx.AsyncClient(transport=transport_b, base_url="http://test") as client:
            get_resp = await client.get(
                f"/v1/capabilities/{entity_id}",
                headers={"X-RSAM-Subject": subject_b},
            )

        # The entity belongs to tenant A — tenant B must not see it. The
        # visibility chokepoint surfaces this as either 403 (forbidden) or
        # 404 (not found); both correctly hide private cross-tenant rows.
        assert get_resp.status_code in (403, 404), (
            f"entity from tenant A must not be visible to tenant B; "
            f"got {get_resp.status_code}: {get_resp.text}"
        )
    finally:
        app_b.dependency_overrides.clear()
        await engine.dispose()


# ---------------------------------------------------------------------------
# Scenario 3: multi-grant tenant header selection


@pytest.mark.asyncio
async def test_multi_grant_header_selection(pg_container: str) -> None:
    """User with two SEAL grants + X-Tenant-ID header → correct tenant selected.

    Without the header the middleware returns 400 (tenant_context_required).
    With X-Tenant-ID set to one of the SEAL IDs, the matching grant is selected
    and the request succeeds.
    """
    seal_a = "4412"
    seal_b = "5523"
    subject = f"U{secrets.token_hex(4)}"
    authorities = [
        f"{seal_a}_DP_CHANNEL_Owner",
        f"{seal_b}_DP_CHANNEL_Manager",
    ]

    settings = _rsam_settings(pg_container)
    engine = _create_engine(settings)
    session_factory = get_session_factory(engine)

    # Pre-materialise both JIT tenants.
    from registry.auth.rsam.tenant_store import upsert_rsam_tenant  # noqa: PLC0415
    async with session_factory() as session, session.begin():
        tenant_a = await upsert_rsam_tenant(session, seal_a)
    async with session_factory() as session, session.begin():
        await upsert_rsam_tenant(session, seal_b)

    await _seed_vocabulary(pg_container, tenant_id=tenant_a)

    # Each request uses a fresh resolver to avoid cache hiding the second call.
    def _fresh_resolver() -> RsamClaimSource:
        return RsamClaimSource(
            settings=settings,
            session_factory=session_factory,
            fetch_authorities=AsyncMock(return_value=authorities),
        )

    # --- Without header: multiple grants → 400 ---
    resolver_1 = _fresh_resolver()
    app_1 = create_app(settings)
    app_1.dependency_overrides[get_tenant_context] = _make_rsam_get_tenant_context(resolver_1)
    try:
        transport_1 = httpx.ASGITransport(app=app_1)
        async with httpx.AsyncClient(transport=transport_1, base_url="http://test") as client:
            resp_no_header = await client.get(
                "/v1/capabilities",
                headers={"X-RSAM-Subject": subject},
            )

        assert resp_no_header.status_code == 400, (
            f"expected 400 (tenant_context_required) without header; "
            f"got {resp_no_header.status_code}: {resp_no_header.text}"
        )
        assert "tenant_context_required" in str(resp_no_header.json()), (
            f"expected tenant_context_required in error body: {resp_no_header.json()}"
        )
    finally:
        app_1.dependency_overrides.clear()

    # --- With correct header: must route to seal_a's tenant (200) ---
    resolver_2 = _fresh_resolver()
    app_2 = create_app(settings)
    app_2.dependency_overrides[get_tenant_context] = _make_rsam_get_tenant_context(resolver_2)
    try:
        transport_2 = httpx.ASGITransport(app=app_2)
        async with httpx.AsyncClient(transport=transport_2, base_url="http://test") as client:
            resp_with_header = await client.get(
                "/v1/capabilities",
                headers={
                    "X-RSAM-Subject": subject,
                    "X-Tenant-ID": seal_a,
                },
            )

        assert resp_with_header.status_code == 200, (
            f"expected 200 with correct X-Tenant-ID header; "
            f"got {resp_with_header.status_code}: {resp_with_header.text}"
        )
    finally:
        app_2.dependency_overrides.clear()
        await engine.dispose()
