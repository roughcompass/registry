"""Integration tests for soft-delete idempotency (RFC 9110 §9.3.5).

Covers:
- First DELETE on a live capability → 204 No Content.
- Second DELETE on the same (now-invalidated) capability → 204 No Content (idempotent).
- DELETE on a never-existing ID → 404 Not Found.
- POST-tunneled :delete alias returns same status codes as the REST verb.

Uses a real Postgres container via the session-scoped ``pg_container`` fixture
in conftest.py.  Each test creates its own tenant + token to avoid state leakage.
"""

from __future__ import annotations

import datetime
import secrets
import uuid
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.api.auth.tokens import hash_token
from registry.config import Settings
from registry.main import create_app
from registry.storage.models import Actor, ApiToken, Tenant
from registry.types import FakeClock

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app(app_settings: Settings, fake_clock: FakeClock) -> Iterator[FastAPI]:
    from registry.api.middleware.tenant import get_clock  # noqa: PLC0415

    _app = create_app(app_settings)
    _app.dependency_overrides[get_clock] = lambda: fake_clock
    yield _app
    _app.dependency_overrides.clear()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


async def _seed(pg_url: str, *, slug: str, roles: list[str]) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Seed tenant + actor + token.  Returns (tenant_id, actor_id, raw_token)."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    raw = secrets.token_urlsafe(24)
    now = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    try:
        async with factory() as session, session.begin():
            session.add(
                Tenant(
                    tenant_id=tenant_id,
                    slug=slug,
                    display_name=slug,
                    created_at=now,
                    is_active=True,
                )
            )
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
            for kind, value in [
                ("entity_type", "capability"),
                ("entity_type", "concept"),
                ("entity_type", "operation"),
                ("fact_category", "overview"),
                ("edge_rel", "concept_of"),
                ("edge_rel", "operation_of"),
                ("edge_rel", "depends_on"),
                ("edge_rel", "replaced_by"),
            ]:
                await session.execute(
                    text(
                        "INSERT INTO vocabulary_values (tenant_id, kind, value, is_system) "
                        "VALUES (:tid, :kind, :value, FALSE)"
                        " ON CONFLICT DO NOTHING"
                    ),
                    {"tid": tenant_id, "kind": kind, "value": value},
                )
    finally:
        await engine.dispose()
    return tenant_id, actor_id, raw


# ---------------------------------------------------------------------------
# REST DELETE idempotency
# ---------------------------------------------------------------------------


class TestRestDeleteIdempotency:
    @pytest.mark.asyncio
    async def test_first_delete_returns_204(self, client: TestClient, pg_container: str) -> None:
        """First DELETE on a live capability row → 204 No Content."""
        _tid, _aid, token = await _seed(
            pg_container,
            slug=f"del-idem-first-{uuid.uuid4().hex[:6]}",
            roles=["producer"],
        )
        auth = {"Authorization": f"Bearer {token}"}

        r = client.post("/v1/capabilities", json={"name": "cap-delete-test"}, headers=auth)
        assert r.status_code == 201, r.text
        entity_id = r.json()["entity_id"]

        d1 = client.delete(f"/v1/capabilities/{entity_id}", headers=auth)
        assert d1.status_code == 204, f"First DELETE must return 204, got {d1.status_code}"

    @pytest.mark.asyncio
    async def test_second_delete_on_invalidated_row_returns_204(self, client: TestClient, pg_container: str) -> None:
        """Second DELETE on an already-invalidated (soft-deleted) row → 204 (idempotent)."""
        _tid, _aid, token = await _seed(
            pg_container,
            slug=f"del-idem-repeat-{uuid.uuid4().hex[:6]}",
            roles=["producer"],
        )
        auth = {"Authorization": f"Bearer {token}"}

        r = client.post("/v1/capabilities", json={"name": "cap-idempotent-delete"}, headers=auth)
        assert r.status_code == 201, r.text
        entity_id = r.json()["entity_id"]

        # First delete: live row → 204.
        d1 = client.delete(f"/v1/capabilities/{entity_id}", headers=auth)
        assert d1.status_code == 204, f"First DELETE must return 204, got {d1.status_code}"

        # Second delete: already-invalidated → must also return 204 (RFC 9110 idempotency).
        d2 = client.delete(f"/v1/capabilities/{entity_id}", headers=auth)
        assert (
            d2.status_code == 204
        ), f"Second DELETE on invalidated row must return 204 (idempotent), got {d2.status_code}"

    @pytest.mark.asyncio
    async def test_delete_never_existing_id_returns_404(self, client: TestClient, pg_container: str) -> None:
        """DELETE on a never-existing UUID → 404 Not Found."""
        _tid, _aid, token = await _seed(
            pg_container,
            slug=f"del-idem-ghost-{uuid.uuid4().hex[:6]}",
            roles=["producer"],
        )
        auth = {"Authorization": f"Bearer {token}"}
        ghost_id = uuid.uuid4()

        d = client.delete(f"/v1/capabilities/{ghost_id}", headers=auth)
        assert d.status_code == 404, f"DELETE on never-existing ID must return 404, got {d.status_code}"


# ---------------------------------------------------------------------------
# POST-tunneled :delete alias — same status code contract
# ---------------------------------------------------------------------------


class TestPostAliasDeleteIdempotency:
    @pytest.mark.asyncio
    async def test_post_alias_first_delete_returns_204(self, client: TestClient, pg_container: str) -> None:
        """POST /v1/capabilities/{id}:delete on live row → 204."""
        _tid, _aid, token = await _seed(
            pg_container,
            slug=f"del-alias-first-{uuid.uuid4().hex[:6]}",
            roles=["producer"],
        )
        auth = {"Authorization": f"Bearer {token}"}

        r = client.post("/v1/capabilities", json={"name": "cap-alias-del"}, headers=auth)
        assert r.status_code == 201, r.text
        entity_id = r.json()["entity_id"]

        d = client.post(f"/v1/capabilities/{entity_id}:delete", headers=auth)
        assert d.status_code == 204, f"POST alias :delete on live row must return 204, got {d.status_code}"

    @pytest.mark.asyncio
    async def test_post_alias_repeat_delete_returns_204(self, client: TestClient, pg_container: str) -> None:
        """POST /v1/capabilities/{id}:delete on already-invalidated row → 204."""
        _tid, _aid, token = await _seed(
            pg_container,
            slug=f"del-alias-repeat-{uuid.uuid4().hex[:6]}",
            roles=["producer"],
        )
        auth = {"Authorization": f"Bearer {token}"}

        r = client.post("/v1/capabilities", json={"name": "cap-alias-repeat-del"}, headers=auth)
        assert r.status_code == 201, r.text
        entity_id = r.json()["entity_id"]

        # First delete via REST verb.
        d1 = client.delete(f"/v1/capabilities/{entity_id}", headers=auth)
        assert d1.status_code == 204

        # Second delete via POST alias → should still be 204.
        d2 = client.post(f"/v1/capabilities/{entity_id}:delete", headers=auth)
        assert d2.status_code == 204, f"POST alias :delete on invalidated row must return 204, got {d2.status_code}"

    @pytest.mark.asyncio
    async def test_post_alias_never_existing_returns_404(self, client: TestClient, pg_container: str) -> None:
        """POST /v1/capabilities/{id}:delete on never-existing ID → 404."""
        _tid, _aid, token = await _seed(
            pg_container,
            slug=f"del-alias-ghost-{uuid.uuid4().hex[:6]}",
            roles=["producer"],
        )
        auth = {"Authorization": f"Bearer {token}"}
        ghost_id = uuid.uuid4()

        d = client.post(f"/v1/capabilities/{ghost_id}:delete", headers=auth)
        assert d.status_code == 404, f"POST alias :delete on ghost ID must return 404, got {d.status_code}"
