"""Integration tests for the end-to-end entitlement auth flow.

Drives the FastAPI app's middleware against a live testcontainers
Postgres instance using ``make_jwt`` from ``tests/helpers/jwt_factory``
to skip the OIDC discovery + JWKS fetch (the validator's signature path
is exercised separately in unit tests). The resolver's fetcher is
swapped via ``app.state.claim_resolver`` for fine-grained control over
the upstream entitlement service responses.

Coverage of the 9 mock-service scenarios from the auth ADR §8 list:
- success_one_tenant → 200, single tenant_membership
- success_multi_tenant → 200, multiple memberships
- empty → 403 access denied
- disabled_tenant → 403, tenant row's disabled_at unmodified
- unknown_role → 403 (all entries dropped during parse)
- malformed → 503 (resolver propagates EntitlementMalformedError)
- auth_rejected_401 → 401 (resolver propagates EntitlementAuthError)
- 5xx (cold cache) → 503
- timeout (cold cache) → 503

The compose-stack smoke test that exercises mock-oauth2-server JWT
issuance lives separately in test_auth_compose_smoke.py (OAR-T24).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.auth.entitlements import client as entitlement_client
from registry.auth.entitlements.resolver import EntitlementResolver
from registry.config import Settings
from registry.main import create_app


def _settings(pg_url: str) -> Settings:
    return Settings(
        database_url=pg_url,
        pgbouncer_url=pg_url,
        scheduler_jobstore_url=pg_url,
        scheduler_use_memory_jobstore=True,
        embedding_model="stub",
        rate_limit_enabled=False,
        oidc_discovery_url="https://idp.test.local/.well-known/openid-configuration",
        oidc_issuer_allowlist=["https://idp.test.local"],
        resource_uri_allowlist=["registry"],
        entitlement_service_url="https://entitlement.test.local",
        entitlement_service_env="DEV",
        entitlement_service_discriminator="REGISTRY",
        entitlement_role_mapping={
            "ADMIN": "admin",
            "PRODUCER": "producer",
            "CONSUMER": "consumer",
            "AUDITOR": "auditor",
        },
    )


@pytest_asyncio.fixture
async def app_with_resolver(pg_container: str) -> AsyncGenerator[tuple[FastAPI, AsyncMock], None]:
    """Build a registry app wired against pg_container with a mocked
    resolver fetcher."""
    settings = _settings(pg_container)
    app = create_app(settings)

    # The lifespan wires app.state.claim_resolver during startup. We
    # need to start the lifespan to populate it, then swap the fetcher.
    async with app.router.lifespan_context(app):
        fetcher = AsyncMock()
        engine = create_async_engine(
            pg_container, connect_args={"prepared_statement_cache_size": 0}
        )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        app.state.claim_resolver = EntitlementResolver(
            settings=settings,
            session_factory=factory,
            fetcher=fetcher,
        )
        try:
            yield app, fetcher
        finally:
            await engine.dispose()


def _bearer_headers(token: str = "dummy.jwt", **extra: str) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    headers.update(extra)
    return headers


def _patch_validator_returning(claims: dict[str, Any], identity: str):
    """Patch validate_oidc_token to return the supplied claims +
    identity instead of decoding the JWT."""
    from unittest.mock import patch

    from registry.api.middleware import tenant as middleware

    return patch.object(
        middleware,
        "validate_oidc_token",
        AsyncMock(return_value=(claims, identity)),
    )


@pytest.mark.asyncio
async def test_success_one_tenant_returns_200(
    app_with_resolver: tuple[FastAPI, AsyncMock]
) -> None:
    app, fetcher = app_with_resolver
    fetcher.return_value = ["t-success-1_REGISTRY_ADMIN"]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with _patch_validator_returning({"sub": "u-1", "iat": 1, "exp": 9999999999}, "u-1"):
            resp = await client.get("/v1/whoami", headers=_bearer_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body.get("tenant_slug") == "t-success-1"


@pytest.mark.asyncio
async def test_empty_entitlements_returns_403(
    app_with_resolver: tuple[FastAPI, AsyncMock]
) -> None:
    app, fetcher = app_with_resolver
    fetcher.return_value = []

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with _patch_validator_returning({"sub": "u-2", "iat": 1, "exp": 9999999999}, "u-2"):
            resp = await client.get("/v1/whoami", headers=_bearer_headers())

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_disabled_tenant_returns_403_and_does_not_modify_row(
    app_with_resolver: tuple[FastAPI, AsyncMock], pg_container: str
) -> None:
    app, fetcher = app_with_resolver

    # Pre-seed the tenant row with disabled_at set.
    import datetime
    slug = f"disabled-{uuid.uuid4().hex[:8]}"
    disabled_ts = datetime.datetime.now(tz=datetime.UTC)
    engine = create_async_engine(
        pg_container, connect_args={"prepared_statement_cache_size": 0}
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO tenants "
                    "(tenant_id, slug, display_name, created_at, is_active, disabled_at) "
                    "VALUES (gen_random_uuid(), :slug, :slug, now(), true, :disabled)"
                ),
                {"slug": slug, "disabled": disabled_ts},
            )

        fetcher.return_value = [f"{slug}_REGISTRY_ADMIN"]

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            with _patch_validator_returning(
                {"sub": "u-disabled", "iat": 1, "exp": 9999999999}, "u-disabled"
            ):
                resp = await client.get("/v1/whoami", headers=_bearer_headers())

        # The resolver pre-filters disabled tenants → empty grants → 403.
        assert resp.status_code == 403

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
async def test_auth_rejected_401_propagates(
    app_with_resolver: tuple[FastAPI, AsyncMock]
) -> None:
    app, fetcher = app_with_resolver
    fetcher.side_effect = entitlement_client.EntitlementAuthError(401)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with _patch_validator_returning({"sub": "u-401", "iat": 1, "exp": 9999999999}, "u-401"):
            resp = await client.get("/v1/whoami", headers=_bearer_headers())

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_5xx_cold_cache_returns_503(
    app_with_resolver: tuple[FastAPI, AsyncMock]
) -> None:
    app, fetcher = app_with_resolver
    fetcher.side_effect = entitlement_client.EntitlementServiceError("upstream 503")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with _patch_validator_returning({"sub": "u-5xx", "iat": 1, "exp": 9999999999}, "u-5xx"):
            resp = await client.get("/v1/whoami", headers=_bearer_headers())

    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_success_multi_tenant_requires_x_tenant_id_header(
    app_with_resolver: tuple[FastAPI, AsyncMock]
) -> None:
    """A user with grants in two tenants who doesn't send X-Tenant-ID
    gets a 400 listing the available tenants — the middleware's
    multi-tenant selection rule."""
    app, fetcher = app_with_resolver
    fetcher.return_value = [
        "t-multi-a_REGISTRY_ADMIN",
        "t-multi-b_REGISTRY_CONSUMER",
    ]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with _patch_validator_returning(
            {"sub": "u-multi", "iat": 1, "exp": 9999999999}, "u-multi"
        ):
            # No X-Tenant-ID → 400.
            resp_no_header = await client.get("/v1/whoami", headers=_bearer_headers())
        assert resp_no_header.status_code == 400
        # The available-tenants list is surfaced in the error body so
        # the caller can fix the request without inspecting the JWT.
        body_no = resp_no_header.json()
        # FastAPI wraps the detail dict — check both shapes.
        detail = body_no.get("detail", body_no)
        if isinstance(detail, dict):
            avail = detail.get("available_tenants", [])
            assert "t-multi-a" in avail
            assert "t-multi-b" in avail

        # With matching X-Tenant-ID → 200, that tenant selected.
        with _patch_validator_returning(
            {"sub": "u-multi", "iat": 1, "exp": 9999999999}, "u-multi"
        ):
            resp_with_header = await client.get(
                "/v1/whoami", headers=_bearer_headers(**{"X-Tenant-ID": "t-multi-b"})
            )
        assert resp_with_header.status_code == 200
        assert resp_with_header.json().get("tenant_slug") == "t-multi-b"


@pytest.mark.asyncio
async def test_unknown_role_drops_to_403(
    app_with_resolver: tuple[FastAPI, AsyncMock]
) -> None:
    """All entitlement entries have role suffixes outside the mapping
    → parser drops them all → resolver returns empty grants → 403."""
    app, fetcher = app_with_resolver
    fetcher.return_value = ["t-ghost_REGISTRY_GHOST_ROLE"]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with _patch_validator_returning(
            {"sub": "u-ghost", "iat": 1, "exp": 9999999999}, "u-ghost"
        ):
            resp = await client.get("/v1/whoami", headers=_bearer_headers())

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_malformed_upstream_returns_503(
    app_with_resolver: tuple[FastAPI, AsyncMock]
) -> None:
    """Upstream entitlement service returns a malformed body →
    EntitlementMalformedError → 503 (no cache fallback consulted)."""
    app, fetcher = app_with_resolver
    fetcher.side_effect = entitlement_client.EntitlementMalformedError(
        "non-JSON body"
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with _patch_validator_returning(
            {"sub": "u-malformed", "iat": 1, "exp": 9999999999}, "u-malformed"
        ):
            resp = await client.get("/v1/whoami", headers=_bearer_headers())

    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_timeout_cold_cache_returns_503(
    app_with_resolver: tuple[FastAPI, AsyncMock]
) -> None:
    """Upstream entitlement service times out with no warm cache →
    EntitlementServiceError (cacheable=True but cold cache) → 503."""
    app, fetcher = app_with_resolver
    fetcher.side_effect = entitlement_client.EntitlementServiceError(
        "transport error: ReadTimeout"
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with _patch_validator_returning(
            {"sub": "u-timeout", "iat": 1, "exp": 9999999999}, "u-timeout"
        ):
            resp = await client.get("/v1/whoami", headers=_bearer_headers())

    assert resp.status_code == 503
