"""Integration tests for soft-delete idempotency (RFC 9110 §9.3.5).

Covers:
- First DELETE on a live capability → 204 No Content.
- Second DELETE on the same (now-invalidated) capability → 204 No Content (idempotent).
- DELETE on a never-existing ID → 404 Not Found.
- POST-tunneled :delete alias returns same status codes as the REST verb.

Uses a real Postgres container via the session-scoped ``pg_container`` fixture
in conftest.py. Each test creates its own tenant + token to avoid state leakage.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    TenantPersona,
    bearer_headers,
    patch_validator_for_actor,
)

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def harness(pg_container: str) -> AsyncIterator[EntitlementAuthHarness]:
    async with EntitlementAuthHarness(pg_container) as h:
        yield h


async def _seed_vocabulary(pg_url: str, tenant_slug: str) -> None:
    """Seed minimum vocabulary for a JIT-materialised tenant."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            row = (
                await session.execute(
                    text("SELECT tenant_id FROM tenants WHERE slug = :slug"),
                    {"slug": tenant_slug},
                )
            ).first()
            assert row is not None, f"tenant {tenant_slug} not materialised yet"
            tenant_id = row[0]
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
                        "VALUES (:tid, :kind, :value, FALSE) "
                        "ON CONFLICT DO NOTHING"
                    ),
                    {"tid": tenant_id, "kind": kind, "value": value},
                )
    finally:
        await engine.dispose()


async def _make_persona(
    h: EntitlementAuthHarness, pg_url: str, *, slug: str, roles: list[str]
) -> TenantPersona:
    """Add a persona, materialise the tenant via a no-op call, seed vocab."""
    persona = h.add_persona(slug, roles=roles)
    h.configure_fetcher_for(persona)
    transport = ASGITransport(app=h.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch_validator_for_actor(persona):
            resp = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))
            assert resp.status_code == 200, resp.text
    await _seed_vocabulary(pg_url, slug)
    return persona


# ---------------------------------------------------------------------------
# REST DELETE idempotency
# ---------------------------------------------------------------------------


class TestRestDeleteIdempotency:
    @pytest.mark.asyncio
    async def test_first_delete_returns_204(self, harness: EntitlementAuthHarness, pg_container: str) -> None:
        """First DELETE on a live capability row → 204 No Content."""
        persona = await _make_persona(
            harness, pg_container, slug=f"del-idem-first-{uuid.uuid4().hex[:6]}", roles=["producer"]
        )
        transport = ASGITransport(app=harness.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            harness.configure_fetcher_for(persona)
            with patch_validator_for_actor(persona):
                r = await client.post(
                    "/v1/capabilities",
                    json={"name": "cap-delete-test"},
                    headers=bearer_headers(tenant_slug=persona.slug),
                )
                assert r.status_code == 201, r.text
                entity_id = r.json()["entity_id"]

                d1 = await client.delete(
                    f"/v1/capabilities/{entity_id}",
                    headers=bearer_headers(tenant_slug=persona.slug),
                )
                assert d1.status_code == 204, f"First DELETE must return 204, got {d1.status_code}"

    @pytest.mark.asyncio
    async def test_second_delete_on_invalidated_row_returns_204(
        self, harness: EntitlementAuthHarness, pg_container: str
    ) -> None:
        """Second DELETE on an already-invalidated (soft-deleted) row → 204 (idempotent)."""
        persona = await _make_persona(
            harness, pg_container, slug=f"del-idem-repeat-{uuid.uuid4().hex[:6]}", roles=["producer"]
        )
        transport = ASGITransport(app=harness.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            harness.configure_fetcher_for(persona)
            with patch_validator_for_actor(persona):
                r = await client.post(
                    "/v1/capabilities",
                    json={"name": "cap-idempotent-delete"},
                    headers=bearer_headers(tenant_slug=persona.slug),
                )
                assert r.status_code == 201, r.text
                entity_id = r.json()["entity_id"]

                d1 = await client.delete(
                    f"/v1/capabilities/{entity_id}",
                    headers=bearer_headers(tenant_slug=persona.slug),
                )
                assert d1.status_code == 204, f"First DELETE must return 204, got {d1.status_code}"

                d2 = await client.delete(
                    f"/v1/capabilities/{entity_id}",
                    headers=bearer_headers(tenant_slug=persona.slug),
                )
                assert d2.status_code == 204, (
                    f"Second DELETE on invalidated row must return 204 (idempotent), got {d2.status_code}"
                )

    @pytest.mark.asyncio
    async def test_delete_never_existing_id_returns_404(
        self, harness: EntitlementAuthHarness, pg_container: str
    ) -> None:
        """DELETE on a never-existing UUID → 404 Not Found."""
        persona = await _make_persona(
            harness, pg_container, slug=f"del-idem-ghost-{uuid.uuid4().hex[:6]}", roles=["producer"]
        )
        transport = ASGITransport(app=harness.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            harness.configure_fetcher_for(persona)
            with patch_validator_for_actor(persona):
                ghost_id = uuid.uuid4()
                d = await client.delete(
                    f"/v1/capabilities/{ghost_id}",
                    headers=bearer_headers(tenant_slug=persona.slug),
                )
                assert d.status_code == 404, f"DELETE on never-existing ID must return 404, got {d.status_code}"


# ---------------------------------------------------------------------------
# POST-tunneled :delete alias — same status code contract
# ---------------------------------------------------------------------------


class TestPostAliasDeleteIdempotency:
    @pytest.mark.asyncio
    async def test_post_alias_first_delete_returns_204(
        self, harness: EntitlementAuthHarness, pg_container: str
    ) -> None:
        """POST /v1/capabilities/{id}:delete on live row → 204."""
        persona = await _make_persona(
            harness, pg_container, slug=f"del-alias-first-{uuid.uuid4().hex[:6]}", roles=["producer"]
        )
        transport = ASGITransport(app=harness.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            harness.configure_fetcher_for(persona)
            with patch_validator_for_actor(persona):
                r = await client.post(
                    "/v1/capabilities",
                    json={"name": "cap-alias-del"},
                    headers=bearer_headers(tenant_slug=persona.slug),
                )
                assert r.status_code == 201, r.text
                entity_id = r.json()["entity_id"]

                d = await client.post(
                    f"/v1/capabilities/{entity_id}:delete",
                    headers=bearer_headers(tenant_slug=persona.slug),
                )
                assert d.status_code == 204, f"POST alias :delete on live row must return 204, got {d.status_code}"

    @pytest.mark.asyncio
    async def test_post_alias_repeat_delete_returns_204(
        self, harness: EntitlementAuthHarness, pg_container: str
    ) -> None:
        """POST /v1/capabilities/{id}:delete on already-invalidated row → 204."""
        persona = await _make_persona(
            harness, pg_container, slug=f"del-alias-repeat-{uuid.uuid4().hex[:6]}", roles=["producer"]
        )
        transport = ASGITransport(app=harness.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            harness.configure_fetcher_for(persona)
            with patch_validator_for_actor(persona):
                r = await client.post(
                    "/v1/capabilities",
                    json={"name": "cap-alias-repeat-del"},
                    headers=bearer_headers(tenant_slug=persona.slug),
                )
                assert r.status_code == 201, r.text
                entity_id = r.json()["entity_id"]

                d1 = await client.delete(
                    f"/v1/capabilities/{entity_id}",
                    headers=bearer_headers(tenant_slug=persona.slug),
                )
                assert d1.status_code == 204

                d2 = await client.post(
                    f"/v1/capabilities/{entity_id}:delete",
                    headers=bearer_headers(tenant_slug=persona.slug),
                )
                assert d2.status_code == 204, (
                    f"POST alias :delete on invalidated row must return 204, got {d2.status_code}"
                )

    @pytest.mark.asyncio
    async def test_post_alias_never_existing_returns_404(
        self, harness: EntitlementAuthHarness, pg_container: str
    ) -> None:
        """POST /v1/capabilities/{id}:delete on never-existing ID → 404."""
        persona = await _make_persona(
            harness, pg_container, slug=f"del-alias-ghost-{uuid.uuid4().hex[:6]}", roles=["producer"]
        )
        transport = ASGITransport(app=harness.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            harness.configure_fetcher_for(persona)
            with patch_validator_for_actor(persona):
                ghost_id = uuid.uuid4()
                d = await client.post(
                    f"/v1/capabilities/{ghost_id}:delete",
                    headers=bearer_headers(tenant_slug=persona.slug),
                )
                assert d.status_code == 404, f"POST alias :delete on ghost ID must return 404, got {d.status_code}"
