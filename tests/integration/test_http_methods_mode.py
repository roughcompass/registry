"""Integration tests for REGISTRY_HTTP_METHODS_MODE across all ported routers.

Covers:

- mode=both (default): PATCH verb route and POST-tunneled alias both reachable;
  byte-identical JSON responses for PATCH /v1/capabilities/{id} and
  POST /v1/capabilities/{id}:update with the same body.
- mode=post_only: PATCH /v1/capabilities/{id} → 405; POST alias → 200.
- mode=rest: POST alias → 404/405; PATCH → 200.

The app is rebuilt per test class so the env var is captured at module-load
time by get_mode_settings().  Async httpx AsyncClient is used throughout.

The delete-idempotency assertions that require a real DB are in
test_delete_idempotency.py.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from registry.config import Settings
from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    TenantPersona,
    bearer_headers,
    patch_validator_for_actor,
)

# ---------------------------------------------------------------------------
# Helpers — mode-app builder + persona seeding
# ---------------------------------------------------------------------------


def _build_mode_app(mode: str, pg_container: str, app_settings: Settings) -> object:
    """Build a full app with the specified HTTP methods mode.

    ``get_mode_settings()`` reads env vars at module-level when each router is
    first imported.  To switch mode, we set the env var and reload the affected
    router modules so ``get_mode_settings()`` is re-evaluated.  ``create_app``
    uses local (inline) imports, so it picks up the reloaded modules from
    ``sys.modules``.

    The env var is restored to "both" after the app is built so that subsequent
    tests do not inherit this test's mode.
    """
    import importlib  # noqa: PLC0415

    import registry.api.routers.admin as _admin
    import registry.api.routers.admin_lifecycle as _adm_life
    import registry.api.routers.admin_pii as _adm_pii
    import registry.api.routers.admin_sync as _adm_sync
    import registry.api.routers.admin_vocab as _adm_vocab

    # Every router that uses HttpMethodRouter or get_mode_settings reads the
    # env var at module-import time. Reload all so the env var is re-evaluated.
    # Missing one here would cause a stale mode to leak into the OpenAPI spec.
    import registry.api.routers.adoptions as _adoptions
    import registry.api.routers.annotations as _ann
    import registry.api.routers.artifacts as _art
    import registry.api.routers.capabilities as _cap
    import registry.api.routers.concepts as _con
    import registry.api.routers.external_ids as _ext_ids
    import registry.api.routers.graph as _graph
    import registry.api.routers.operations as _ops
    import registry.api.routers.subscriptions as _subs
    import registry.api.routers.workspaces as _ws

    # Reload leaf modules first so admin.py's re-exports point at fresh routers.
    _to_reload = [
        _cap,
        _con,
        _ops,
        _art,
        _graph,
        _adm_life,
        _adm_pii,
        _adm_sync,
        _adm_vocab,
        _adoptions,
        _ann,
        _ext_ids,
        _subs,
        _ws,
        _admin,
    ]

    prev_mode = os.environ.get("REGISTRY_HTTP_METHODS_MODE", "rest")
    prev_sep = os.environ.get("REGISTRY_HTTP_METHOD_ALIAS_SEPARATOR", "colon")
    try:
        os.environ["REGISTRY_HTTP_METHODS_MODE"] = mode
        os.environ["REGISTRY_HTTP_METHOD_ALIAS_SEPARATOR"] = "colon"

        for mod in _to_reload:
            importlib.reload(mod)

        from registry.main import create_app  # noqa: PLC0415

        return create_app(app_settings)
    finally:
        os.environ["REGISTRY_HTTP_METHODS_MODE"] = prev_mode
        os.environ["REGISTRY_HTTP_METHOD_ALIAS_SEPARATOR"] = prev_sep
        for mod in _to_reload:
            importlib.reload(mod)


async def _make_persona_with_harness(
    harness: EntitlementAuthHarness,
    pg_url: str,
    *,
    slug: str,
    roles: list[str],
) -> TenantPersona:
    """Materialise a tenant+actor via /v1/whoami on the harness app."""
    from sqlalchemy import text  # noqa: PLC0415
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: PLC0415

    persona = harness.add_persona(slug, roles=roles)
    harness.configure_fetcher_for(persona)
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch_validator_for_actor(persona):
            resp = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))
            assert resp.status_code == 200, resp.text

    # Seed vocabulary so capability create succeeds.
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            row = (
                await session.execute(
                    text("SELECT tenant_id FROM tenants WHERE slug = :slug"),
                    {"slug": slug},
                )
            ).first()
            assert row is not None
            tenant_id = row[0]
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
                        "VALUES (:tid, :kind, :value, FALSE) ON CONFLICT DO NOTHING"
                    ),
                    {"tid": tenant_id, "kind": kind, "value": value},
                )
    finally:
        await engine.dispose()

    return persona


@pytest_asyncio.fixture
async def harness(pg_container: str) -> AsyncIterator[EntitlementAuthHarness]:
    async with EntitlementAuthHarness(pg_container) as h:
        yield h


# ---------------------------------------------------------------------------
# mode=both (default): byte-identical PATCH / POST-alias responses
# ---------------------------------------------------------------------------


class TestModeBothCapabilities:
    """PATCH and POST alias must both work and produce identical JSON in mode=both."""

    @pytest.mark.asyncio
    async def test_patch_and_alias_byte_identical(
        self, harness: EntitlementAuthHarness, app_settings: Settings, pg_container: str
    ) -> None:
        persona = await _make_persona_with_harness(
            harness,
            pg_container,
            slug=f"http-mode-both-{uuid.uuid4().hex[:6]}",
            roles=["producer"],
        )
        harness.configure_fetcher_for(persona)

        app = _build_mode_app("both", pg_container, app_settings)
        # Wire the harness resolver into the mode app so auth is consistent.
        app.state.claim_resolver = harness.app.state.claim_resolver  # type: ignore[attr-defined]

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            with patch_validator_for_actor(persona):
                r = await client.post(
                    "/v1/capabilities",
                    json={"name": "svc-a"},
                    headers=bearer_headers(tenant_slug=persona.slug),
                )
                assert r.status_code == 201, r.text
                entity_id = r.json()["entity_id"]

                update_body = {"updates": {"name": "svc-a-updated"}}

                r_verb = await client.patch(
                    f"/v1/capabilities/{entity_id}",
                    json=update_body,
                    headers=bearer_headers(tenant_slug=persona.slug),
                )
                assert r_verb.status_code == 200, r_verb.text

                r_alias = await client.post(
                    f"/v1/capabilities/{entity_id}:update",
                    json=update_body,
                    headers=bearer_headers(tenant_slug=persona.slug),
                )
                assert r_alias.status_code == 200, r_alias.text

        assert r_verb.json() == r_alias.json(), (
            "PATCH and POST:update must return byte-identical JSON in mode=both"
        )

    @pytest.mark.asyncio
    async def test_openapi_contains_both_surfaces(
        self, harness: EntitlementAuthHarness, app_settings: Settings, pg_container: str
    ) -> None:
        app = _build_mode_app("both", pg_container, app_settings)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            spec = (await client.get("/openapi.json")).json()
        paths = spec.get("paths", {})
        has_patch = any("patch" in methods for methods in paths.values())
        has_alias = any(":update" in p for p in paths)
        assert has_patch, "PATCH routes must appear in openapi.json for mode=both"
        assert has_alias, "POST alias routes must appear in openapi.json for mode=both"


# ---------------------------------------------------------------------------
# mode=post_only: PATCH → 405, POST alias → 200
# ---------------------------------------------------------------------------


class TestModePostOnly:
    @pytest.mark.asyncio
    async def test_patch_returns_405_in_post_only(
        self, harness: EntitlementAuthHarness, app_settings: Settings, pg_container: str
    ) -> None:
        persona = await _make_persona_with_harness(
            harness,
            pg_container,
            slug=f"http-mode-post-only-{uuid.uuid4().hex[:6]}",
            roles=["producer"],
        )
        harness.configure_fetcher_for(persona)

        app = _build_mode_app("post_only", pg_container, app_settings)
        app.state.claim_resolver = harness.app.state.claim_resolver  # type: ignore[attr-defined]

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            with patch_validator_for_actor(persona):
                r = await client.post(
                    "/v1/capabilities",
                    json={"name": "svc-b"},
                    headers=bearer_headers(tenant_slug=persona.slug),
                )
                assert r.status_code == 201, r.text
                entity_id = r.json()["entity_id"]

                r_patch = await client.patch(
                    f"/v1/capabilities/{entity_id}",
                    json={"updates": {"name": "svc-b-updated"}},
                    headers=bearer_headers(tenant_slug=persona.slug),
                )
        assert r_patch.status_code in (404, 405), (
            f"PATCH must not be reachable in post_only mode, got {r_patch.status_code}"
        )

    @pytest.mark.asyncio
    async def test_post_alias_works_in_post_only(
        self, harness: EntitlementAuthHarness, app_settings: Settings, pg_container: str
    ) -> None:
        persona = await _make_persona_with_harness(
            harness,
            pg_container,
            slug=f"http-mode-post-only2-{uuid.uuid4().hex[:6]}",
            roles=["producer"],
        )
        harness.configure_fetcher_for(persona)

        app = _build_mode_app("post_only", pg_container, app_settings)
        app.state.claim_resolver = harness.app.state.claim_resolver  # type: ignore[attr-defined]

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            with patch_validator_for_actor(persona):
                r = await client.post(
                    "/v1/capabilities",
                    json={"name": "svc-c"},
                    headers=bearer_headers(tenant_slug=persona.slug),
                )
                assert r.status_code == 201, r.text
                entity_id = r.json()["entity_id"]

                r_alias = await client.post(
                    f"/v1/capabilities/{entity_id}:update",
                    json={"updates": {"name": "svc-c-updated"}},
                    headers=bearer_headers(tenant_slug=persona.slug),
                )
        assert r_alias.status_code == 200, r_alias.text
        assert r_alias.json()["name"] == "svc-c-updated"

    @pytest.mark.asyncio
    async def test_openapi_no_patch_in_post_only(
        self, harness: EntitlementAuthHarness, app_settings: Settings, pg_container: str
    ) -> None:
        app = _build_mode_app("post_only", pg_container, app_settings)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            spec = (await client.get("/openapi.json")).json()
        paths = spec.get("paths", {})
        patch_paths = [p for p, methods in paths.items() if "patch" in methods]
        assert not patch_paths, (
            f"PATCH routes must not appear in openapi.json for mode=post_only: {patch_paths}"
        )


# ---------------------------------------------------------------------------
# mode=rest: POST alias absent, PATCH present
# ---------------------------------------------------------------------------


class TestModeRest:
    @pytest.mark.asyncio
    async def test_patch_works_in_rest(
        self, harness: EntitlementAuthHarness, app_settings: Settings, pg_container: str
    ) -> None:
        persona = await _make_persona_with_harness(
            harness,
            pg_container,
            slug=f"http-mode-rest-{uuid.uuid4().hex[:6]}",
            roles=["producer"],
        )
        harness.configure_fetcher_for(persona)

        app = _build_mode_app("rest", pg_container, app_settings)
        app.state.claim_resolver = harness.app.state.claim_resolver  # type: ignore[attr-defined]

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            with patch_validator_for_actor(persona):
                r = await client.post(
                    "/v1/capabilities",
                    json={"name": "svc-d"},
                    headers=bearer_headers(tenant_slug=persona.slug),
                )
                assert r.status_code == 201, r.text
                entity_id = r.json()["entity_id"]

                r_patch = await client.patch(
                    f"/v1/capabilities/{entity_id}",
                    json={"updates": {"name": "svc-d-updated"}},
                    headers=bearer_headers(tenant_slug=persona.slug),
                )
        assert r_patch.status_code == 200, r_patch.text

    @pytest.mark.asyncio
    async def test_post_alias_absent_in_rest(
        self, harness: EntitlementAuthHarness, app_settings: Settings, pg_container: str
    ) -> None:
        persona = await _make_persona_with_harness(
            harness,
            pg_container,
            slug=f"http-mode-rest2-{uuid.uuid4().hex[:6]}",
            roles=["producer"],
        )
        harness.configure_fetcher_for(persona)

        app = _build_mode_app("rest", pg_container, app_settings)
        app.state.claim_resolver = harness.app.state.claim_resolver  # type: ignore[attr-defined]

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            with patch_validator_for_actor(persona):
                r = await client.post(
                    "/v1/capabilities",
                    json={"name": "svc-e"},
                    headers=bearer_headers(tenant_slug=persona.slug),
                )
                assert r.status_code == 201, r.text
                entity_id = r.json()["entity_id"]

                r_alias = await client.post(
                    f"/v1/capabilities/{entity_id}:update",
                    json={"updates": {"name": "svc-e-updated"}},
                    headers=bearer_headers(tenant_slug=persona.slug),
                )
        assert r_alias.status_code in (404, 405), (
            f"POST alias must not be reachable in rest mode, got {r_alias.status_code}"
        )
