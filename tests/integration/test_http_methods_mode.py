"""Integration tests for REGISTRY_HTTP_METHODS_MODE across all ported routers.

Covers:

- mode=both (default): PATCH verb route and POST-tunneled alias both reachable;
  byte-identical JSON responses for PATCH /v1/capabilities/{id} and
  POST /v1/capabilities/{id}:update with the same body.
- mode=post_only: PATCH /v1/capabilities/{id} → 405; POST alias → 200.
- mode=rest: POST alias → 404/405; PATCH → 200.

The app is rebuilt per test class so the env var is captured at module-load
time by get_mode_settings().  TestClient (sync ASGI) is used because we need
the full router wiring without a real database for the mode tests.

The delete-idempotency assertions that require a real DB are in
test_delete_idempotency.py.
"""

from __future__ import annotations

import datetime
import os
import secrets
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.api.auth.tokens import hash_token
from registry.config import Settings
from registry.storage.models import Actor, ApiToken, Tenant

# ---------------------------------------------------------------------------
# Helpers — shared seeding + minimal mock-app builder
# ---------------------------------------------------------------------------


def _build_mode_app(mode: str, pg_container: str, app_settings: Settings) -> FastAPI:
    """Build a full Capability Catalog app with the specified HTTP methods mode.

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
    import registry.api.routers.admin_tokens as _adm_tok
    import registry.api.routers.admin_vocab as _adm_vocab

    # Every router that uses HttpMethodRouter or get_mode_settings reads the
    # env var at module-import time. To switch mode, reload all of them so
    # the env var is re-evaluated. Missing one here is the cluster-D defect:
    # the un-reloaded router keeps the previously-set mode and leaks PATCH
    # routes into post_only-mode openapi.
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

    # Order matters: admin.py is a facade that imports mutation_router /
    # admin_mutation_router instances from each admin_*.py submodule. If admin
    # is reloaded BEFORE its submodules, it captures the still-stale router
    # instances and they leak into the include chain. Reload all leaf modules
    # first, then admin last so its re-exports point at the fresh routers.
    _to_reload = [
        _cap,
        _con,
        _ops,
        _art,
        _graph,
        _adm_life,
        _adm_pii,
        _adm_sync,
        _adm_tok,
        _adm_vocab,
        _adoptions,
        _ann,
        _ext_ids,
        _subs,
        _ws,
        _admin,
    ]

    # Default mode is "rest"; fallback string matches the current default.
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
        # Restore modules to the default so subsequent tests are unaffected.
        for mod in _to_reload:
            importlib.reload(mod)


async def _seed(pg_url: str, *, slug: str, roles: list[str]) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Insert tenant + actor + API token and return (tenant_id, actor_id, raw_token)."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    raw = secrets.token_urlsafe(24)
    now = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    from sqlalchemy import text  # noqa: PLC0415

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
# mode=both (default): byte-identical PATCH / POST-alias responses
# ---------------------------------------------------------------------------


class TestModeBothCapabilities:
    """PATCH and POST alias must both work and produce identical JSON in mode=both."""

    @pytest.mark.asyncio
    async def test_patch_and_alias_byte_identical(self, app_settings: Settings, pg_container: str) -> None:
        _tid, _aid, token = await _seed(
            pg_container,
            slug=f"http-mode-both-{uuid.uuid4().hex[:6]}",
            roles=["producer"],
        )
        auth = {"Authorization": f"Bearer {token}"}

        app = _build_mode_app("both", pg_container, app_settings)
        with TestClient(app) as client:
            # Create a capability to update.
            r = client.post(
                "/v1/capabilities",
                json={"name": "svc-a"},
                headers=auth,
            )
            assert r.status_code == 201, r.text
            entity_id = r.json()["entity_id"]

            update_body = {"updates": {"name": "svc-a-updated"}}

            # REST verb surface.
            r_verb = client.patch(
                f"/v1/capabilities/{entity_id}",
                json=update_body,
                headers=auth,
            )
            assert r_verb.status_code == 200, r_verb.text

            # POST-tunneled alias surface.
            r_alias = client.post(
                f"/v1/capabilities/{entity_id}:update",
                json=update_body,
                headers=auth,
            )
            assert r_alias.status_code == 200, r_alias.text

            assert r_verb.json() == r_alias.json(), "PATCH and POST:update must return byte-identical JSON in mode=both"

    @pytest.mark.asyncio
    async def test_openapi_contains_both_surfaces(self, app_settings: Settings, pg_container: str) -> None:
        app = _build_mode_app("both", pg_container, app_settings)
        with TestClient(app) as client:
            spec = client.get("/openapi.json").json()
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
    async def test_patch_returns_405_in_post_only(self, app_settings: Settings, pg_container: str) -> None:
        _tid, _aid, token = await _seed(
            pg_container,
            slug=f"http-mode-post-only-{uuid.uuid4().hex[:6]}",
            roles=["producer"],
        )
        auth = {"Authorization": f"Bearer {token}"}

        app = _build_mode_app("post_only", pg_container, app_settings)
        with TestClient(app, raise_server_exceptions=False) as client:
            # Create a capability via the always-registered POST.
            r = client.post(
                "/v1/capabilities",
                json={"name": "svc-b"},
                headers=auth,
            )
            assert r.status_code == 201, r.text
            entity_id = r.json()["entity_id"]

            # PATCH must not be registered in post_only mode.
            r_patch = client.patch(
                f"/v1/capabilities/{entity_id}",
                json={"updates": {"name": "svc-b-updated"}},
                headers=auth,
            )
            assert r_patch.status_code in (
                404,
                405,
            ), f"PATCH must not be reachable in post_only mode, got {r_patch.status_code}"

    @pytest.mark.asyncio
    async def test_post_alias_works_in_post_only(self, app_settings: Settings, pg_container: str) -> None:
        _tid, _aid, token = await _seed(
            pg_container,
            slug=f"http-mode-post-only2-{uuid.uuid4().hex[:6]}",
            roles=["producer"],
        )
        auth = {"Authorization": f"Bearer {token}"}

        app = _build_mode_app("post_only", pg_container, app_settings)
        with TestClient(app) as client:
            r = client.post(
                "/v1/capabilities",
                json={"name": "svc-c"},
                headers=auth,
            )
            assert r.status_code == 201, r.text
            entity_id = r.json()["entity_id"]

            r_alias = client.post(
                f"/v1/capabilities/{entity_id}:update",
                json={"updates": {"name": "svc-c-updated"}},
                headers=auth,
            )
            assert r_alias.status_code == 200, r_alias.text
            assert r_alias.json()["name"] == "svc-c-updated"

    @pytest.mark.asyncio
    async def test_openapi_no_patch_in_post_only(self, app_settings: Settings, pg_container: str) -> None:
        app = _build_mode_app("post_only", pg_container, app_settings)
        with TestClient(app) as client:
            spec = client.get("/openapi.json").json()
        paths = spec.get("paths", {})
        patch_paths = [p for p, methods in paths.items() if "patch" in methods]
        assert not patch_paths, f"PATCH routes must not appear in openapi.json for mode=post_only: {patch_paths}"


# ---------------------------------------------------------------------------
# mode=rest: POST alias absent, PATCH present
# ---------------------------------------------------------------------------


class TestModeRest:
    @pytest.mark.asyncio
    async def test_patch_works_in_rest(self, app_settings: Settings, pg_container: str) -> None:
        _tid, _aid, token = await _seed(
            pg_container,
            slug=f"http-mode-rest-{uuid.uuid4().hex[:6]}",
            roles=["producer"],
        )
        auth = {"Authorization": f"Bearer {token}"}

        app = _build_mode_app("rest", pg_container, app_settings)
        with TestClient(app) as client:
            r = client.post(
                "/v1/capabilities",
                json={"name": "svc-d"},
                headers=auth,
            )
            assert r.status_code == 201, r.text
            entity_id = r.json()["entity_id"]

            r_patch = client.patch(
                f"/v1/capabilities/{entity_id}",
                json={"updates": {"name": "svc-d-updated"}},
                headers=auth,
            )
            assert r_patch.status_code == 200, r_patch.text

    @pytest.mark.asyncio
    async def test_post_alias_absent_in_rest(self, app_settings: Settings, pg_container: str) -> None:
        _tid, _aid, token = await _seed(
            pg_container,
            slug=f"http-mode-rest2-{uuid.uuid4().hex[:6]}",
            roles=["producer"],
        )
        auth = {"Authorization": f"Bearer {token}"}

        app = _build_mode_app("rest", pg_container, app_settings)
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.post(
                "/v1/capabilities",
                json={"name": "svc-e"},
                headers=auth,
            )
            assert r.status_code == 201, r.text
            entity_id = r.json()["entity_id"]

            r_alias = client.post(
                f"/v1/capabilities/{entity_id}:update",
                json={"updates": {"name": "svc-e-updated"}},
                headers=auth,
            )
            assert r_alias.status_code in (
                404,
                405,
            ), f"POST alias must not be reachable in rest mode, got {r_alias.status_code}"
