"""Unit tests for HttpMethodRouter.

Coverage
--------
- mode=rest      : verb route registered; POST alias absent → 405 via test client.
- mode=post_only : POST alias registered; verb route absent → 405 via test client.
- mode=both      : both routes registered; handler callable from either surface.
- Separator colon: alias path uses ':'.
- Separator slash: alias path uses '/'.
- Byte-identical response from verb and POST alias in mode=both.
- OpenAPI spec reflects active mode (mutation paths present/absent).
- soft_delete_response_code: 204 on first delete; 204 on already-invalidated; 404 on not-found / hard-purged.
- get_mode_settings: reads env vars; falls back on invalid values.
- Invalid mode / separator rejected at construction time with ValueError.
- add_read_route and add_create_route always register regardless of mode.

No I/O, no network, no database required.

The default mode is "rest"; operators behind enterprise gateways that strip
non-GET/POST verbs can opt into "both" or "post_only" via REGISTRY_HTTP_METHODS_MODE.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import APIRouter, FastAPI, Response, status
from fastapi.testclient import TestClient

from registry.api.middleware.http_methods import (
    HttpMethodRouter,
    get_mode_settings,
    soft_delete_response_code,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_PAYLOAD = {"result": "ok", "value": 42}
_BODY_BYTES = b'{"result":"ok","value":42}'


def _build_app(mode: str, separator: str = "colon") -> FastAPI:
    """Build a minimal FastAPI app with a single mutation route registered via
    HttpMethodRouter so we can exercise all three modes end-to-end."""

    app = FastAPI()
    base = APIRouter(prefix="/v1/items")
    mr = HttpMethodRouter(base, mode=mode, separator=separator)  # type: ignore[arg-type]

    async def _handler() -> dict[str, Any]:
        return _PAYLOAD

    async def _create() -> dict[str, Any]:
        return {"created": True}

    async def _read() -> dict[str, Any]:
        return {"item": "data"}

    # Mutation route under test
    mr.add_mutation_route(
        path="/{item_id}",
        action="update",
        handler=_handler,
        verb="PATCH",
        response_model=None,
    )
    # Read and create (always registered)
    mr.add_read_route("/{item_id}", _read)
    mr.add_create_route("", _create, status_code=201)

    app.include_router(base)
    return app


def _client(mode: str, separator: str = "colon") -> TestClient:
    return TestClient(_build_app(mode, separator), raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# mode=rest
# ---------------------------------------------------------------------------


class TestModeRest:
    def test_verb_route_reachable(self) -> None:
        client = _client("rest")
        r = client.patch("/v1/items/abc123")
        assert r.status_code == 200
        assert r.json() == _PAYLOAD

    def test_post_alias_not_registered(self) -> None:
        client = _client("rest")
        r = client.post("/v1/items/abc123:update")
        # FastAPI returns 404 for unknown paths (not 405) when the path is not registered.
        assert r.status_code in (404, 405)

    def test_openapi_has_patch_no_alias(self) -> None:
        app = _build_app("rest")
        client = TestClient(app)
        spec = client.get("/openapi.json").json()
        paths = spec.get("paths", {})
        assert any(
            "patch" in methods for methods in paths.values()
        ), "PATCH route must appear in openapi.json for mode=rest"
        # No path should contain ':update' or '/update' tunneled alias
        for path in paths:
            assert (
                ":update" not in path and "/update" not in path.split("/v1/items/")[-1]
            ), f"POST alias path found in openapi.json for mode=rest: {path}"


# ---------------------------------------------------------------------------
# mode=post_only
# ---------------------------------------------------------------------------


class TestModePostOnly:
    def test_post_alias_reachable_colon(self) -> None:
        client = _client("post_only", "colon")
        r = client.post("/v1/items/abc123:update")
        assert r.status_code == 200
        assert r.json() == _PAYLOAD

    def test_post_alias_reachable_slash(self) -> None:
        client = _client("post_only", "slash")
        r = client.post("/v1/items/abc123/update")
        assert r.status_code == 200
        assert r.json() == _PAYLOAD

    def test_verb_route_not_registered(self) -> None:
        client = _client("post_only")
        r = client.patch("/v1/items/abc123")
        assert r.status_code in (404, 405)

    def test_openapi_has_alias_no_patch(self) -> None:
        app = _build_app("post_only")
        client = TestClient(app)
        spec = client.get("/openapi.json").json()
        paths = spec.get("paths", {})
        alias_paths = [p for p in paths if ":update" in p]
        assert alias_paths, "POST alias must appear in openapi.json for mode=post_only"
        patch_paths = [p for p in paths if "patch" in paths[p]]
        assert not patch_paths, "PATCH route must not appear in openapi.json for mode=post_only"


# ---------------------------------------------------------------------------
# mode=both
# ---------------------------------------------------------------------------


class TestModeBoth:
    def test_verb_route_reachable(self) -> None:
        client = _client("both")
        r = client.patch("/v1/items/abc123")
        assert r.status_code == 200

    def test_post_alias_reachable(self) -> None:
        client = _client("both")
        r = client.post("/v1/items/abc123:update")
        assert r.status_code == 200

    def test_byte_identical_response(self) -> None:
        """Both surfaces must call the same handler and return identical JSON."""
        client = _client("both")
        r_verb = client.patch("/v1/items/abc123")
        r_alias = client.post("/v1/items/abc123:update")
        assert r_verb.json() == r_alias.json(), "Verb and POST-alias must return identical JSON in mode=both"

    def test_openapi_has_both_surfaces(self) -> None:
        app = _build_app("both")
        client = TestClient(app)
        spec = client.get("/openapi.json").json()
        paths = spec.get("paths", {})
        has_patch = any("patch" in methods for methods in paths.values())
        has_alias = any(":update" in p for p in paths)
        assert has_patch, "PATCH route must appear in openapi.json for mode=both"
        assert has_alias, "POST alias must appear in openapi.json for mode=both"


# ---------------------------------------------------------------------------
# Separator variants
# ---------------------------------------------------------------------------


class TestSeparator:
    def test_colon_separator_path(self) -> None:
        """POST /v1/items/{id}:update reachable with separator=colon."""
        client = _client("both", "colon")
        r = client.post("/v1/items/x:update")
        assert r.status_code == 200

    def test_slash_separator_path(self) -> None:
        """POST /v1/items/{id}/update reachable with separator=slash."""
        client = _client("both", "slash")
        r = client.post("/v1/items/x/update")
        assert r.status_code == 200

    def test_colon_alias_absent_when_slash(self) -> None:
        """Colon alias must NOT exist when separator=slash."""
        client = _client("both", "slash")
        r = client.post("/v1/items/x:update")
        assert r.status_code in (404, 405)

    def test_slash_alias_absent_when_colon(self) -> None:
        """Slash alias must NOT exist when separator=colon (only alias path is :update)."""
        # With colon, the path becomes /v1/items/{item_id}:update
        # The item_id captures everything up to the colon, so /update would be a sub-path.
        client = _client("both", "colon")
        r = client.post("/v1/items/x/update")
        assert r.status_code in (404, 405)


# ---------------------------------------------------------------------------
# Always-registered routes (read + create)
# ---------------------------------------------------------------------------


class TestAlwaysRegisteredRoutes:
    @pytest.mark.parametrize("mode", ["rest", "post_only", "both"])
    def test_get_always_registered(self, mode: str) -> None:
        client = _client(mode)
        r = client.get("/v1/items/abc123")
        assert r.status_code == 200

    @pytest.mark.parametrize("mode", ["rest", "post_only", "both"])
    def test_create_always_registered(self, mode: str) -> None:
        client = _client(mode)
        r = client.post("/v1/items")
        assert r.status_code == 201


# ---------------------------------------------------------------------------
# Construction guards
# ---------------------------------------------------------------------------


class TestConstructionGuards:
    def test_invalid_mode_raises(self) -> None:
        with pytest.raises(ValueError, match="mode must be one of"):
            HttpMethodRouter(APIRouter(), mode="unknown")  # type: ignore[arg-type]

    def test_invalid_separator_raises(self) -> None:
        with pytest.raises(ValueError, match="separator must be one of"):
            HttpMethodRouter(APIRouter(), mode="both", separator="pipe")  # type: ignore[arg-type]

    def test_router_property(self) -> None:
        base = APIRouter()
        mr = HttpMethodRouter(base, mode="rest")
        assert mr.router is base

    def test_mode_property(self) -> None:
        mr = HttpMethodRouter(APIRouter(), mode="post_only")
        assert mr.mode == "post_only"

    def test_separator_property(self) -> None:
        mr = HttpMethodRouter(APIRouter(), mode="both", separator="slash")
        assert mr.separator == "slash"


# ---------------------------------------------------------------------------
# DELETE idempotency via soft_delete_response_code
# ---------------------------------------------------------------------------


class TestSoftDeleteResponseCode:
    def test_first_delete_live_row_returns_204(self) -> None:
        code = soft_delete_response_code(found=True, already_invalidated=False)
        assert code == 204

    def test_repeat_delete_already_invalidated_returns_204(self) -> None:
        """Second DELETE on soft-deleted row is idempotent → 204."""
        code = soft_delete_response_code(found=True, already_invalidated=True)
        assert code == 204

    def test_never_existing_id_returns_404(self) -> None:
        code = soft_delete_response_code(found=False, already_invalidated=False)
        assert code == 404

    def test_hard_purged_row_returns_404(self) -> None:
        """Hard-purged (RTBF / crypto-shred) row: row found but data gone → 404."""
        code = soft_delete_response_code(found=True, already_invalidated=True, hard_purged=True)
        assert code == 404

    def test_hard_purged_live_row_returns_404(self) -> None:
        """hard_purged=True overrides found=True even if not yet invalidated."""
        code = soft_delete_response_code(found=True, already_invalidated=False, hard_purged=True)
        assert code == 404


# ---------------------------------------------------------------------------
# DELETE idempotency end-to-end via test client
# ---------------------------------------------------------------------------


def _build_delete_app(mode: str) -> FastAPI:
    """Build a tiny app to test DELETE idempotency semantics end-to-end."""
    from fastapi import FastAPI

    app = FastAPI()
    base = APIRouter(prefix="/v1/things")
    mr = HttpMethodRouter(base, mode=mode, separator="colon")

    # Simulate: known_ids = {live_id, invalidated_id}; purged_id not in DB.
    live_id = "live-001"
    invalidated_id = "invalidated-001"

    async def _delete(thing_id: str) -> Response:
        if thing_id == live_id:
            # First call: live → 204
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        if thing_id == invalidated_id:
            # Repeat call: already soft-deleted → 204 (idempotent)
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        # Not found / hard-purged
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    mr.add_mutation_route(
        path="/{thing_id}",
        action="delete",
        handler=_delete,
        verb="DELETE",
        response_class=Response,
    )
    app.include_router(base)
    return app


class TestDeleteIdempotencyE2E:
    def test_first_delete_live_row_204(self) -> None:
        client = TestClient(_build_delete_app("rest"))
        r = client.delete("/v1/things/live-001")
        assert r.status_code == 204

    def test_repeat_delete_already_invalidated_204(self) -> None:
        client = TestClient(_build_delete_app("rest"))
        r = client.delete("/v1/things/invalidated-001")
        assert r.status_code == 204

    def test_delete_never_existing_404(self) -> None:
        client = TestClient(_build_delete_app("rest"))
        r = client.delete("/v1/things/ghost-999")
        assert r.status_code == 404

    def test_post_alias_delete_live_row_204(self) -> None:
        client = TestClient(_build_delete_app("both"))
        r = client.post("/v1/things/live-001:delete")
        assert r.status_code == 204

    def test_post_alias_delete_already_invalidated_204(self) -> None:
        client = TestClient(_build_delete_app("both"))
        r = client.post("/v1/things/invalidated-001:delete")
        assert r.status_code == 204

    def test_post_alias_delete_never_existing_404(self) -> None:
        client = TestClient(_build_delete_app("both"))
        r = client.post("/v1/things/ghost-999:delete")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# get_mode_settings — env var reading
# ---------------------------------------------------------------------------


class TestGetModeSettings:
    def test_defaults_when_no_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("REGISTRY_HTTP_METHODS_MODE", raising=False)
        monkeypatch.delenv("REGISTRY_HTTP_METHOD_ALIAS_SEPARATOR", raising=False)
        mode, sep = get_mode_settings()
        # Default is 'rest'; POST-tunneled aliases are opt-in via REGISTRY_HTTP_METHODS_MODE=both.
        assert mode == "rest"
        assert sep == "colon"

    def test_reads_valid_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REGISTRY_HTTP_METHODS_MODE", "rest")
        monkeypatch.delenv("REGISTRY_HTTP_METHOD_ALIAS_SEPARATOR", raising=False)
        mode, sep = get_mode_settings()
        assert mode == "rest"

    def test_reads_valid_separator(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("REGISTRY_HTTP_METHODS_MODE", raising=False)
        monkeypatch.setenv("REGISTRY_HTTP_METHOD_ALIAS_SEPARATOR", "slash")
        _, sep = get_mode_settings()
        assert sep == "slash"

    def test_invalid_mode_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REGISTRY_HTTP_METHODS_MODE", "graphql")
        monkeypatch.delenv("REGISTRY_HTTP_METHOD_ALIAS_SEPARATOR", raising=False)
        mode, _ = get_mode_settings()
        # Fallback is 'rest' (matches Settings.http_methods_mode default).
        assert mode == "rest"

    def test_invalid_separator_falls_back_to_colon(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("REGISTRY_HTTP_METHODS_MODE", raising=False)
        monkeypatch.setenv("REGISTRY_HTTP_METHOD_ALIAS_SEPARATOR", "pipe")
        _, sep = get_mode_settings()
        assert sep == "colon"

    def test_mode_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REGISTRY_HTTP_METHODS_MODE", "POST_ONLY")
        monkeypatch.delenv("REGISTRY_HTTP_METHOD_ALIAS_SEPARATOR", raising=False)
        mode, _ = get_mode_settings()
        assert mode == "post_only"

    def test_all_valid_modes_readable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for m in ("rest", "post_only", "both"):
            monkeypatch.setenv("REGISTRY_HTTP_METHODS_MODE", m)
            mode, _ = get_mode_settings()
            assert mode == m

    def test_all_valid_separators_readable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for s in ("colon", "slash"):
            monkeypatch.setenv("REGISTRY_HTTP_METHOD_ALIAS_SEPARATOR", s)
            _, sep = get_mode_settings()
            assert sep == s
