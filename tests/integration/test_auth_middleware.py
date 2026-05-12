"""Auth middleware integration tests.

Covers: /healthz and /readyz health probes; 401 rejection for invalid tokens,
missing tokens, and expired tokens; cross-tenant isolation (tenant A's token
never resolves to tenant B's context).

All tests target the real FastAPI app constructed via ``create_app(settings)``
with a single test-only protected route (``GET /v1/_whoami``) mounted to
exercise the auth middleware end-to-end.
"""

from __future__ import annotations

import datetime
import secrets
import uuid
from collections.abc import Iterator

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.api.auth.tokens import hash_token
from registry.api.middleware.tenant import get_clock, get_tenant_context
from registry.config import Settings
from registry.main import create_app
from registry.storage.models import Actor, ApiToken, Tenant
from registry.types import Clock, FakeClock, TenantContext


@pytest.fixture
def app(app_settings: Settings, fake_clock: FakeClock) -> Iterator[FastAPI]:
    """Build the production app and add a single protected `_whoami` route."""
    app = create_app(app_settings)

    @app.get("/v1/_whoami")
    async def _whoami(ctx: TenantContext = Depends(get_tenant_context)) -> dict[str, str]:
        return {"tenant_id": str(ctx.tenant_id), "actor_id": str(ctx.actor_id)}

    app.dependency_overrides[get_clock] = lambda: fake_clock
    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def test_healthz_ok(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_db_up(client: TestClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 200


def test_invalid_token_401(client: TestClient) -> None:
    response = client.get("/v1/_whoami", headers={"Authorization": "Bearer not-a-real-token"})
    assert response.status_code == 401


def test_missing_token_401(client: TestClient) -> None:
    response = client.get("/v1/_whoami")
    assert response.status_code == 401


async def _seed_tenant_with_token(
    pg_container: str,
    *,
    tenant_slug: str,
    expires_at: datetime.datetime | None = None,
    revoked_at: datetime.datetime | None = None,
) -> tuple[uuid.UUID, str]:
    """Insert a tenant + actor + api_token row directly. Returns (tenant_id, plaintext_token)."""
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    raw_token = secrets.token_urlsafe(24)
    now = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    try:
        async with factory() as session, session.begin():
            session.add(
                Tenant(tenant_id=tenant_id, slug=tenant_slug, display_name=tenant_slug, created_at=now, is_active=True)
            )
            await session.flush()
            session.add(
                Actor(
                    actor_id=actor_id,
                    tenant_id=tenant_id,
                    display_name=f"actor-{tenant_slug}",
                    email=None,
                    oidc_subject=None,
                    created_at=now,
                )
            )
            await session.flush()
            session.add(
                ApiToken(
                    token_id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    token_hash=hash_token(raw_token),
                    roles=["producer"],
                    description=None,
                    expires_at=expires_at,
                    created_at=now,
                    revoked_at=revoked_at,
                )
            )
    finally:
        await engine.dispose()
    return tenant_id, raw_token


@pytest.mark.asyncio
async def test_expired_token_401(client: TestClient, pg_container: str) -> None:
    expired = datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC)  # well before fake_clock = 2026-01-01
    _tenant, raw_token = await _seed_tenant_with_token(pg_container, tenant_slug="expired-co", expires_at=expired)
    response = client.get("/v1/_whoami", headers={"Authorization": f"Bearer {raw_token}"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_cross_tenant_isolation(client: TestClient, pg_container: str) -> None:
    """Tenant A's token never resolves to tenant B's tenant_id, and vice versa."""
    tenant_a, token_a = await _seed_tenant_with_token(pg_container, tenant_slug="alpha-co")
    tenant_b, token_b = await _seed_tenant_with_token(pg_container, tenant_slug="beta-co")

    response_a = client.get("/v1/_whoami", headers={"Authorization": f"Bearer {token_a}"})
    response_b = client.get("/v1/_whoami", headers={"Authorization": f"Bearer {token_b}"})

    assert response_a.status_code == 200
    assert response_b.status_code == 200
    assert response_a.json()["tenant_id"] == str(tenant_a)
    assert response_b.json()["tenant_id"] == str(tenant_b)
    assert response_a.json()["tenant_id"] != response_b.json()["tenant_id"]


# Silence unused-import warnings for the FakeClock and Clock annotations imported
# above — they are referenced by the fixture protocol contract documented in
# `fabric/api/middleware/tenant.py`.
_ = (AsyncSession, Clock)
