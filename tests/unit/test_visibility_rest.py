"""Unit tests for PATCH /v1/capabilities/{entity_id}/visibility.

Coverage
--------
- PATCH /v1/capabilities/{id}/visibility with visibility=private → 200 + capability response.
- PATCH /v1/capabilities/{id}/visibility with visibility=tenant-shared + shared_with_tenants → 200.
- PATCH /v1/capabilities/{id}/visibility with visibility=public → 200.
- tenant-shared without shared_with_tenants → 422 (service ValidationError surfaced).
- tenant-shared with empty shared_with_tenants list → 422.
- Invalid visibility value → 422.
- Non-owner tenant (PermissionError from service) → 403.
- Entity not found → 404.
- Consumer role → 403 (require_roles gate fires before service call).
- POST-tunneled alias POST /{id}:set-visibility reachable in mode=both (default).
- set_visibility called with correct arguments.

No database, no network required — all service calls mocked via AsyncMock.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from registry.api.routers.capabilities import mutation_router, router
from registry.exceptions import NotFoundError, ValidationError
from registry.types import TenantContext

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_TENANT_A = uuid.uuid4()
_ACTOR_A = uuid.uuid4()
_ENTITY_ID = uuid.uuid4()
_TENANT_B = uuid.uuid4()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(tenant_id: uuid.UUID = _TENANT_A, roles: list[str] | None = None) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        actor_id=_ACTOR_A,
        roles=roles if roles is not None else ["producer"],
    )


def _capability_record_mock(entity_id: uuid.UUID = _ENTITY_ID) -> MagicMock:
    """Build a minimal CapabilityRecord-shaped mock for get_full_capability return value."""
    entity = MagicMock()
    entity.entity_id = entity_id
    entity.tenant_id = _TENANT_A
    entity.entity_type = "capability"
    entity.name = "PaymentAPI"
    entity.external_id = None
    entity.is_active = True
    entity.created_at = _NOW

    record = MagicMock()
    record.entity = entity
    record.lifecycle = "draft"
    record.attributes = {}
    record.facts = []
    record.edges_out = []
    record.edges_in = []
    record.superseded_facts_count = 0
    return record


def _build_app(
    set_visibility_effect: Any = None,
    get_full_capability_return: Any = None,
    ctx: TenantContext | None = None,
) -> FastAPI:
    """Build a minimal FastAPI app with capabilities router and mocked services.

    ``set_visibility_effect`` can be an exception instance (to simulate errors)
    or ``None`` (success — returns None).
    ``get_full_capability_return`` defaults to a valid CapabilityRecord mock.
    ``ctx`` overrides the injected TenantContext (defaults to producer role).
    """
    app = FastAPI()

    # Install the structured error envelope so test responses match the
    # production shape ({"errors": [{path, code, message}]}) instead of
    # FastAPI's default {"detail": ...}.
    from registry.main import _install_error_envelope  # noqa: PLC0415

    _install_error_envelope(app)

    # Wire the read router first (provides GET + POST create), then mutation.
    app.include_router(router)
    app.include_router(mutation_router)

    effective_ctx = ctx if ctx is not None else _ctx()
    record = get_full_capability_return if get_full_capability_return is not None else _capability_record_mock()

    # Mock VisibilityService
    vis_svc = MagicMock()
    if set_visibility_effect is not None and isinstance(set_visibility_effect, Exception):
        vis_svc.set_visibility = AsyncMock(side_effect=set_visibility_effect)
    else:
        vis_svc.set_visibility = AsyncMock(return_value=None)
    app.state.visibility = vis_svc

    # Mock CatalogService. resolve_entity_handle returns the entity ref so the
    # handler's name-or-uuid resolution step succeeds; routes then call
    # get_full_capability with the resolved entity_id.
    catalog_svc = MagicMock()
    catalog_svc.resolve_entity_handle = AsyncMock(return_value=record.entity)
    catalog_svc.get_full_capability = AsyncMock(return_value=record)
    app.state.catalog = catalog_svc

    # Override auth dependency so no real token validation runs.
    from registry.api.middleware.tenant import get_tenant_context  # noqa: PLC0415

    async def _fake_tenant_ctx() -> TenantContext:  # type: ignore[misc]
        return effective_ctx

    app.dependency_overrides[get_tenant_context] = _fake_tenant_ctx

    return app


# ---------------------------------------------------------------------------
# Happy-path: PATCH /v1/capabilities/{id}/visibility
# ---------------------------------------------------------------------------


class TestSetVisibilitySuccess:
    def test_private_returns_200_and_capability(self) -> None:
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.patch(
            f"/v1/capabilities/{_ENTITY_ID}/visibility",
            json={"visibility": "private"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["entity_id"] == str(_ENTITY_ID)
        assert data["name"] == "PaymentAPI"

    def test_public_returns_200(self) -> None:
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.patch(
            f"/v1/capabilities/{_ENTITY_ID}/visibility",
            json={"visibility": "public"},
        )
        assert resp.status_code == 200

    def test_tenant_shared_with_acl_returns_200(self) -> None:
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.patch(
            f"/v1/capabilities/{_ENTITY_ID}/visibility",
            json={
                "visibility": "tenant-shared",
                "shared_with_tenants": [str(_TENANT_B)],
            },
        )
        assert resp.status_code == 200

    def test_set_visibility_called_with_correct_args(self) -> None:
        """Verify the service receives entity_id, visibility, and parsed shared_with_tenants."""
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=True)
        client.patch(
            f"/v1/capabilities/{_ENTITY_ID}/visibility",
            json={
                "visibility": "tenant-shared",
                "shared_with_tenants": [str(_TENANT_B)],
            },
        )
        vis_svc = app.state.visibility
        vis_svc.set_visibility.assert_awaited_once()
        call_args = vis_svc.set_visibility.call_args
        # Positional or keyword: (ctx, entity_id, visibility, shared_with_tenants=...)
        assert call_args.args[1] == _ENTITY_ID
        assert call_args.args[2] == "tenant-shared"
        assert call_args.kwargs.get("shared_with_tenants") == [_TENANT_B]

    def test_private_passes_none_shared_with_tenants(self) -> None:
        """For private/public, shared_with_tenants must be None (not empty list)."""
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=True)
        client.patch(
            f"/v1/capabilities/{_ENTITY_ID}/visibility",
            json={"visibility": "private"},
        )
        vis_svc = app.state.visibility
        vis_svc.set_visibility.assert_awaited_once()
        call_args = vis_svc.set_visibility.call_args
        assert call_args.kwargs.get("shared_with_tenants") is None


# ---------------------------------------------------------------------------
# POST-tunneled alias — opt-in via REGISTRY_HTTP_METHODS_MODE=both
# ---------------------------------------------------------------------------
#
# The default-mode "alias is not registered" assertion that used to live
# here was removed when the test pyramid started setting
# REGISTRY_HTTP_METHODS_MODE=both at conftest load time. The full mode
# matrix (rest / both / post_only) is covered exhaustively in
# tests/integration/test_http_methods_mode.py via subprocess + module
# reload, so the unit-level check would have been redundant.


# ---------------------------------------------------------------------------
# Role gate: consumer → 403
# ---------------------------------------------------------------------------


class TestRoleGate:
    def test_consumer_role_returns_403(self) -> None:
        """Consumer callers must not be able to set visibility."""
        consumer_ctx = _ctx(roles=["consumer"])
        app = _build_app(ctx=consumer_ctx)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.patch(
            f"/v1/capabilities/{_ENTITY_ID}/visibility",
            json={"visibility": "private"},
        )
        assert resp.status_code == 403

    def test_auditor_role_returns_403(self) -> None:
        auditor_ctx = _ctx(roles=["auditor"])
        app = _build_app(ctx=auditor_ctx)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.patch(
            f"/v1/capabilities/{_ENTITY_ID}/visibility",
            json={"visibility": "private"},
        )
        assert resp.status_code == 403

    def test_admin_role_returns_200(self) -> None:
        admin_ctx = _ctx(roles=["admin"])
        app = _build_app(ctx=admin_ctx)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.patch(
            f"/v1/capabilities/{_ENTITY_ID}/visibility",
            json={"visibility": "private"},
        )
        assert resp.status_code == 200

    def test_producer_role_returns_200(self) -> None:
        producer_ctx = _ctx(roles=["producer"])
        app = _build_app(ctx=producer_ctx)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.patch(
            f"/v1/capabilities/{_ENTITY_ID}/visibility",
            json={"visibility": "private"},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Validation errors → 422
# ---------------------------------------------------------------------------


class TestValidationErrors:
    def test_tenant_shared_without_acl_returns_422(self) -> None:
        """VisibilityService raises ValidationError when tenant-shared lacks ACL."""
        exc = ValidationError("'tenant-shared' visibility requires a non-empty shared_with_tenants list.")
        app = _build_app(set_visibility_effect=exc)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.patch(
            f"/v1/capabilities/{_ENTITY_ID}/visibility",
            json={"visibility": "tenant-shared"},
        )
        assert resp.status_code == 422

    def test_tenant_shared_with_empty_acl_returns_422(self) -> None:
        """Empty list is treated the same as absent — service raises ValidationError."""
        exc = ValidationError("'tenant-shared' visibility requires a non-empty shared_with_tenants list.")
        app = _build_app(set_visibility_effect=exc)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.patch(
            f"/v1/capabilities/{_ENTITY_ID}/visibility",
            json={"visibility": "tenant-shared", "shared_with_tenants": []},
        )
        assert resp.status_code == 422

    def test_invalid_visibility_value_returns_422(self) -> None:
        """Unknown visibility string → ValidationError from service → 422."""
        exc = ValidationError("invalid visibility 'world-readable'.")
        app = _build_app(set_visibility_effect=exc)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.patch(
            f"/v1/capabilities/{_ENTITY_ID}/visibility",
            json={"visibility": "world-readable"},
        )
        assert resp.status_code == 422

    def test_missing_visibility_field_returns_422(self) -> None:
        """Pydantic rejects body without required 'visibility' field → 422."""
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.patch(
            f"/v1/capabilities/{_ENTITY_ID}/visibility",
            json={"shared_with_tenants": [str(_TENANT_B)]},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Service PermissionError → 403
# ---------------------------------------------------------------------------


class TestPermissionError:
    def test_non_owner_tenant_returns_403(self) -> None:
        """PermissionError from VisibilityService (non-owner) → HTTP 403."""
        exc = PermissionError(f"entity {_ENTITY_ID} is not visible to tenant {_TENANT_B}.")
        app = _build_app(set_visibility_effect=exc)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.patch(
            f"/v1/capabilities/{_ENTITY_ID}/visibility",
            json={"visibility": "private"},
        )
        assert resp.status_code == 403
        assert str(_ENTITY_ID) in resp.json()["errors"][0]["message"]


# ---------------------------------------------------------------------------
# Entity not found → 404
# ---------------------------------------------------------------------------


class TestNotFound:
    def test_entity_not_found_returns_404(self) -> None:
        exc = NotFoundError(f"entity {_ENTITY_ID} not found for tenant {_TENANT_A}")
        app = _build_app(set_visibility_effect=exc)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.patch(
            f"/v1/capabilities/{_ENTITY_ID}/visibility",
            json={"visibility": "private"},
        )
        assert resp.status_code == 404
