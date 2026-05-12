"""Unit tests for LifecycleService.

Covers:
- VALID_TRANSITIONS map structure (no-DB, pure-function checks).
- transition() with successor=<uuid> delegates to CatalogService.create_edge().
- transition() with successor='none' does not call create_edge().
- transition() with no CatalogService injected uses fallback ORM path.
- Admin endpoint enforces admin/producer role; 403 for consumer.
- Admin endpoint maps LifecycleError to HTTP 422.
- New successor parameter shape: 'none' succeeds, UUID succeeds, omitted is 422.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from registry.exceptions import LifecycleError
from registry.service.lifecycle import VALID_TRANSITIONS, LifecycleService
from registry.types import EntityRef, FakeClock, TenantContext

# ---------------------------------------------------------------------------
# VALID_TRANSITIONS — pure-function checks, no DB
# ---------------------------------------------------------------------------


def test_alpha_can_advance_to_beta_deprecated_or_retired() -> None:
    assert VALID_TRANSITIONS["alpha"] == {"beta", "deprecated", "retired"}


def test_beta_can_advance_to_ga_deprecated_or_retired() -> None:
    assert VALID_TRANSITIONS["beta"] == {"ga", "deprecated", "retired"}


def test_ga_can_only_become_deprecated_or_retired() -> None:
    assert VALID_TRANSITIONS["ga"] == {"deprecated", "retired"}


def test_deprecated_can_only_become_retired() -> None:
    assert VALID_TRANSITIONS["deprecated"] == {"retired"}


def test_retired_is_terminal() -> None:
    assert VALID_TRANSITIONS["retired"] == set()


def test_no_state_can_skip_backwards_to_alpha() -> None:
    for state, allowed in VALID_TRANSITIONS.items():
        assert "alpha" not in allowed, f"{state} can illegally regress to alpha"


# ---------------------------------------------------------------------------
# Helpers for LifecycleService.transition() and endpoint tests
# ---------------------------------------------------------------------------

_T0 = datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC)


def _ctx(roles: list[str] | None = None) -> TenantContext:
    return TenantContext(
        tenant_id=uuid.uuid4(),
        actor_id=uuid.uuid4(),
        roles=roles or ["admin"],
    )


def _mock_session_factory(valid_transition: bool = True) -> Any:
    """Return a session_factory that mocks DB checks to allow/deny transitions."""
    session = AsyncMock()
    session.begin = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=None),
            __aexit__=AsyncMock(return_value=False),
        )
    )

    # _enforce_transition query: return None (no current state → first write).
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result_mock)
    session.add = MagicMock()
    session.flush = AsyncMock()

    ctx_mgr = AsyncMock()
    ctx_mgr.__aenter__ = AsyncMock(return_value=session)
    ctx_mgr.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock(return_value=ctx_mgr)
    return factory


# ---------------------------------------------------------------------------
# LifecycleService.transition() — successor=<uuid> delegation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transition_with_successor_uuid_calls_catalog_create_edge() -> None:
    """When successor is a UUID and CatalogService is injected, create_edge is called."""
    ctx = _ctx()
    entity_id = uuid.uuid4()
    successor_id = uuid.uuid4()

    catalog_mock = AsyncMock()
    catalog_mock.create_edge = AsyncMock()

    factory = _mock_session_factory()
    clock = FakeClock(_T0)

    svc = LifecycleService(session_factory=factory, clock=clock, catalog=catalog_mock)

    # Patch the internal _enforce_transition and _write_attribute so we can
    # exercise just the successor delegation path.
    with (
        patch.object(svc, "_enforce_transition", AsyncMock()),
        patch.object(svc, "_write_attribute", AsyncMock()),
    ):
        await svc.transition(ctx, entity_id, "deprecated", successor=successor_id, valid_from=_T0)

    catalog_mock.create_edge.assert_awaited_once_with(
        ctx,
        entity_id,
        "replaced_by",
        successor_id,
        valid_from=_T0,
    )


@pytest.mark.asyncio
async def test_transition_with_successor_none_does_not_call_create_edge() -> None:
    """successor='none' — create_edge must never be called."""
    ctx = _ctx()
    entity_id = uuid.uuid4()

    catalog_mock = AsyncMock()
    catalog_mock.create_edge = AsyncMock()

    factory = _mock_session_factory()
    clock = FakeClock(_T0)

    svc = LifecycleService(session_factory=factory, clock=clock, catalog=catalog_mock)

    with (
        patch.object(svc, "_enforce_transition", AsyncMock()),
        patch.object(svc, "_write_attribute", AsyncMock()),
    ):
        await svc.transition(ctx, entity_id, "deprecated", successor="none")

    catalog_mock.create_edge.assert_not_called()


@pytest.mark.asyncio
async def test_transition_without_catalog_uses_fallback_orm_path() -> None:
    """No CatalogService injected — _upsert_replaced_by_edge must be called."""
    ctx = _ctx()
    entity_id = uuid.uuid4()
    successor_id = uuid.uuid4()

    factory = _mock_session_factory()
    clock = FakeClock(_T0)

    svc = LifecycleService(session_factory=factory, clock=clock, catalog=None)

    upsert_mock = AsyncMock()
    with (
        patch.object(svc, "_enforce_transition", AsyncMock()),
        patch.object(svc, "_write_attribute", AsyncMock()),
        patch.object(svc, "_upsert_replaced_by_edge", upsert_mock),
    ):
        await svc.transition(ctx, entity_id, "deprecated", successor=successor_id)

    upsert_mock.assert_awaited_once()


# ---------------------------------------------------------------------------
# Lifecycle admin endpoint — role enforcement and error mapping
# ---------------------------------------------------------------------------


def _make_app_with_ctx(ctx: TenantContext) -> Any:
    """Build a minimal FastAPI test app that stubs auth to return *ctx*."""
    from fastapi import FastAPI

    from registry.api.routers.admin import lifecycle_router
    from registry.main import _install_error_envelope

    app = FastAPI()
    _install_error_envelope(app)
    app.include_router(lifecycle_router)

    # Stub out app.state. resolve_entity_handle returns an EntityRef whose
    # entity_id echoes whatever UUID was passed (path param is a UUID string
    # in these tests, so the resolve call is identity-like).
    catalog_mock = AsyncMock()

    async def _resolve(ctx: Any, handle: str, **_kw: Any) -> EntityRef:
        return EntityRef(
            entity_id=uuid.UUID(handle),
            tenant_id=ctx.tenant_id,
            entity_type="capability",
            name="test-cap",
            external_id=None,
            is_active=True,
            created_at=_T0,
        )

    catalog_mock.resolve_entity_handle = _resolve
    app.state.session_factory = MagicMock()
    app.state.clock = FakeClock(_T0)
    app.state.catalog = catalog_mock

    # Override get_tenant_context to return our ctx.
    from registry.api.middleware.tenant import get_tenant_context

    app.dependency_overrides[get_tenant_context] = lambda: ctx
    return app


@pytest.mark.asyncio
async def test_lifecycle_endpoint_allowed_for_admin() -> None:
    ctx = _ctx(["admin"])
    app = _make_app_with_ctx(ctx)
    entity_id = uuid.uuid4()

    with (
        patch("catalog.api.routers.admin_lifecycle.LifecycleService") as mock_cls,
    ):
        mock_instance = AsyncMock()
        mock_instance.transition = AsyncMock()
        mock_cls.return_value = mock_instance

        client = TestClient(app, raise_server_exceptions=True)
        resp = client.patch(
            f"/v1/capabilities/{entity_id}/lifecycle",
            json={"new_state": "beta", "successor": "none"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["new_state"] == "beta"


@pytest.mark.asyncio
async def test_lifecycle_endpoint_allowed_for_producer() -> None:
    ctx = _ctx(["producer"])
    app = _make_app_with_ctx(ctx)
    entity_id = uuid.uuid4()

    with patch("catalog.api.routers.admin_lifecycle.LifecycleService") as mock_cls:
        mock_instance = AsyncMock()
        mock_instance.transition = AsyncMock()
        mock_cls.return_value = mock_instance

        client = TestClient(app, raise_server_exceptions=True)
        resp = client.patch(
            f"/v1/capabilities/{entity_id}/lifecycle",
            json={"new_state": "beta", "successor": "none"},
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_lifecycle_endpoint_forbidden_for_consumer() -> None:
    ctx = _ctx(["consumer"])
    app = _make_app_with_ctx(ctx)
    entity_id = uuid.uuid4()

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.patch(
        f"/v1/capabilities/{entity_id}/lifecycle",
        json={"new_state": "beta", "successor": "none"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_lifecycle_endpoint_maps_lifecycle_error_to_422() -> None:
    ctx = _ctx(["admin"])
    app = _make_app_with_ctx(ctx)
    entity_id = uuid.uuid4()

    with patch("catalog.api.routers.admin_lifecycle.LifecycleService") as mock_cls:
        mock_instance = AsyncMock()
        mock_instance.transition = AsyncMock(side_effect=LifecycleError("invalid transition: 'ga' -> 'alpha'"))
        mock_cls.return_value = mock_instance

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.patch(
            f"/v1/capabilities/{entity_id}/lifecycle",
            json={"new_state": "alpha", "successor": "none"},
        )
    assert resp.status_code == 422
    assert "invalid transition" in resp.json()["errors"][0]["message"]


# ---------------------------------------------------------------------------
# Successor parameter shape validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifecycle_endpoint_successor_uuid_accepted() -> None:
    """Passing a valid UUID as successor succeeds at the Pydantic boundary."""
    ctx = _ctx(["admin"])
    app = _make_app_with_ctx(ctx)
    entity_id = uuid.uuid4()
    successor_id = uuid.uuid4()

    with patch("catalog.api.routers.admin_lifecycle.LifecycleService") as mock_cls:
        mock_instance = AsyncMock()
        mock_instance.transition = AsyncMock()
        mock_cls.return_value = mock_instance

        client = TestClient(app, raise_server_exceptions=True)
        resp = client.patch(
            f"/v1/capabilities/{entity_id}/lifecycle",
            json={"new_state": "deprecated", "successor": str(successor_id)},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["replaced_by"] == str(successor_id)


@pytest.mark.asyncio
async def test_lifecycle_endpoint_successor_none_sentinel_accepted() -> None:
    """Passing the sentinel 'none' as successor succeeds and sets replaced_by=null."""
    ctx = _ctx(["admin"])
    app = _make_app_with_ctx(ctx)
    entity_id = uuid.uuid4()

    with patch("catalog.api.routers.admin_lifecycle.LifecycleService") as mock_cls:
        mock_instance = AsyncMock()
        mock_instance.transition = AsyncMock()
        mock_cls.return_value = mock_instance

        client = TestClient(app, raise_server_exceptions=True)
        resp = client.patch(
            f"/v1/capabilities/{entity_id}/lifecycle",
            json={"new_state": "deprecated", "successor": "none"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["replaced_by"] is None


@pytest.mark.asyncio
async def test_lifecycle_endpoint_omitting_successor_is_422() -> None:
    """Omitting the required successor field is rejected with 422."""
    ctx = _ctx(["admin"])
    app = _make_app_with_ctx(ctx)
    entity_id = uuid.uuid4()

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.patch(
        f"/v1/capabilities/{entity_id}/lifecycle",
        json={"new_state": "deprecated"},  # successor intentionally absent
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_lifecycle_endpoint_garbage_successor_string_is_422() -> None:
    """A non-UUID, non-'none' string is rejected at the Pydantic boundary with 422."""
    ctx = _ctx(["admin"])
    app = _make_app_with_ctx(ctx)
    entity_id = uuid.uuid4()

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.patch(
        f"/v1/capabilities/{entity_id}/lifecycle",
        json={"new_state": "deprecated", "successor": "not-a-uuid-and-not-none"},
    )
    assert resp.status_code == 422
