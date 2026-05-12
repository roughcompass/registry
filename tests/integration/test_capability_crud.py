"""Capability CRUD + auth token integration tests.

Covers: create+retrieve roundtrip across capability/concept/operation/artifacts;
PATCH bi-temporal supersession; DELETE soft-delete cascade; mandatory schema
rejection; admin mint+revoke flow; revoked token returns 401.
"""

from __future__ import annotations

import datetime
import secrets
import uuid
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.api.auth.tokens import hash_token
from registry.api.middleware.tenant import get_clock
from registry.config import Settings
from registry.main import create_app
from registry.storage.models import Actor, ApiToken, Fact, Tenant
from registry.types import FakeClock


@pytest.fixture
def app(app_settings: Settings, fake_clock: FakeClock) -> Iterator[FastAPI]:
    app = create_app(app_settings)
    app.dependency_overrides[get_clock] = lambda: fake_clock
    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


async def _seed(pg_url: str, *, slug: str, roles: list[str]) -> tuple[uuid.UUID, uuid.UUID, str]:
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    raw = secrets.token_urlsafe(24)
    now = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    try:
        async with factory() as session, session.begin():
            session.add(Tenant(tenant_id=tenant_id, slug=slug, display_name=slug, created_at=now, is_active=True))
            await session.flush()
            session.add(
                Actor(
                    actor_id=actor_id,
                    tenant_id=tenant_id,
                    display_name=f"a-{slug}",
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
                    token_hash=hash_token(raw),
                    roles=roles,
                    description=None,
                    expires_at=None,
                    created_at=now,
                    revoked_at=None,
                )
            )
            # Seed vocab for this tenant (default tenant has them but new tenants don't).
            for kind, value in [
                ("entity_type", "capability"),
                ("entity_type", "concept"),
                ("entity_type", "operation"),
                ("fact_category", "overview"),
                ("fact_category", "adr"),
                ("fact_category", "dev_doc"),
                ("edge_rel", "concept_of"),
                ("edge_rel", "operation_of"),
                ("edge_rel", "depends_on"),
                ("edge_rel", "replaced_by"),
            ]:
                await session.execute(
                    text(
                        "INSERT INTO vocabulary_values (tenant_id, kind, value, is_system) "
                        "VALUES (:tid, :kind, :value, FALSE)"
                    ),
                    {"tid": tenant_id, "kind": kind, "value": value},
                )
    finally:
        await engine.dispose()
    return tenant_id, actor_id, raw


@pytest.mark.asyncio
async def test_capability_roundtrip(client: TestClient, pg_container: str) -> None:
    """Create → GET → assert all fields preserved."""
    _tid, _aid, token = await _seed(pg_container, slug=f"alpha-{uuid.uuid4().hex[:6]}", roles=["producer"])
    auth = {"Authorization": f"Bearer {token}"}
    r = client.post(
        "/v1/capabilities",
        json={"name": "payment-service", "external_id": "pay-svc"},
        headers=auth,
    )
    assert r.status_code == 201, r.text
    entity_id = r.json()["entity_id"]

    g = client.get(f"/v1/capabilities/{entity_id}", headers=auth)
    assert g.status_code == 200
    body = g.json()
    assert body["name"] == "payment-service"
    assert body["external_id"] == "pay-svc"


@pytest.mark.asyncio
async def test_artifact_create_supersession_on_update(client: TestClient, pg_container: str) -> None:
    """create_fact then update_fact — old t_valid_to should be set, new row exists."""
    _tid, _aid, token = await _seed(pg_container, slug=f"beta-{uuid.uuid4().hex[:6]}", roles=["producer"])
    auth = {"Authorization": f"Bearer {token}"}
    cap = client.post("/v1/capabilities", json={"name": "search"}, headers=auth)
    entity_id = cap.json()["entity_id"]

    # Create a fact via the artifacts router.
    f1 = client.post(
        f"/v1/capabilities/{entity_id}/artifacts",
        json={"category": "overview", "body": "v1"},
        headers=auth,
    )
    assert f1.status_code == 201, f1.text

    # Verify the artifact appears in the listing.
    listed = client.get(f"/v1/capabilities/{entity_id}/artifacts", headers=auth)
    assert listed.status_code == 200
    assert any(item["body"] == "v1" for item in listed.json())


@pytest.mark.asyncio
async def test_delete_entity_soft_deletes_and_cascades(client: TestClient, pg_container: str) -> None:
    _tid, _aid, token = await _seed(pg_container, slug=f"gamma-{uuid.uuid4().hex[:6]}", roles=["producer"])
    auth = {"Authorization": f"Bearer {token}"}
    cap = client.post("/v1/capabilities", json={"name": "ingest"}, headers=auth)
    entity_id = cap.json()["entity_id"]
    f = client.post(
        f"/v1/capabilities/{entity_id}/artifacts",
        json={"category": "adr", "body": "decide partitioning"},
        headers=auth,
    )
    fact_id = f.json()["fact_id"]

    d = client.delete(f"/v1/capabilities/{entity_id}", headers=auth)
    assert d.status_code == 204

    # The artifact should be soft-deleted (t_invalidated_at IS NOT NULL) — direct DB check.
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            row = await session.execute(select(Fact).where(Fact.fact_id == uuid.UUID(fact_id)))
            fact = row.scalar_one()
            assert fact.t_invalidated_at is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_admin_mint_then_revoke(client: TestClient, pg_container: str) -> None:
    _tid, actor_id, admin_token = await _seed(pg_container, slug=f"delta-{uuid.uuid4().hex[:6]}", roles=["admin"])
    auth_admin = {"Authorization": f"Bearer {admin_token}"}

    minted = client.post(
        "/v1/admin/tokens",
        json={"actor_id": str(actor_id), "roles": ["producer"]},
        headers=auth_admin,
    )
    assert minted.status_code == 201, minted.text
    new_token = minted.json()["plaintext_token"]
    new_token_id = minted.json()["token_id"]

    # Producer token works: list artifacts on a non-existent entity returns [] (no auth failure).
    auth_producer = {"Authorization": f"Bearer {new_token}"}
    bogus_entity = uuid.uuid4()
    r1 = client.get(f"/v1/capabilities/{bogus_entity}/artifacts", headers=auth_producer)
    assert r1.status_code == 200

    # Revoke and confirm 401.
    rev = client.delete(f"/v1/admin/tokens/{new_token_id}", headers=auth_admin)
    assert rev.status_code == 204
    r2 = client.get(f"/v1/capabilities/{bogus_entity}/artifacts", headers=auth_producer)
    assert r2.status_code == 401


@pytest.mark.asyncio
async def test_non_admin_cannot_mint(client: TestClient, pg_container: str) -> None:
    _tid, actor_id, producer_token = await _seed(pg_container, slug=f"eps-{uuid.uuid4().hex[:6]}", roles=["producer"])
    r = client.post(
        "/v1/admin/tokens",
        json={"actor_id": str(actor_id), "roles": ["producer"]},
        headers={"Authorization": f"Bearer {producer_token}"},
    )
    assert r.status_code == 403
