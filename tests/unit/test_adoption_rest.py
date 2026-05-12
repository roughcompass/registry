"""Unit tests for adoption REST endpoints.

Service interactions are mocked at ``app.state.adoption``; no DB or
network is involved.

Coverage:
- POST   /v1/capabilities/{cap_id}/adoptions  → 201 default shape (no t_* keys)
- POST   ... ?view=audit                       → 201 with bitemporal fields
- POST   ... ?view=bad                         → 422
- POST   ... not found                         → 404
- POST   ... validation error                  → 422
- GET    /v1/capabilities/{cap_id}/adoptions   → 200 default (empty list)
- GET    ... with adoption                     → 200 list, no t_* keys in default
- GET    ... ?view=audit                       → 200 list with bitemporal fields
- GET    ... ?view=bad                         → 422
- Default response shape must not contain valid_from / ingested_at keys.
"""

from __future__ import annotations

import datetime
import uuid
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from registry.api.routers.adoptions import mutation_router, router
from registry.exceptions import NotFoundError, ValidationError
from registry.types import AdoptionEventRef, EntityRef, TenantContext

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_TENANT = uuid.uuid4()
_ACTOR = uuid.uuid4()
_CAP_ID = uuid.uuid4()
_ADOPTION_ID = uuid.uuid4()
_CONSUMER_TENANT = uuid.uuid4()


def _ctx(roles: list[str] | None = None) -> TenantContext:
    return TenantContext(
        tenant_id=_TENANT,
        actor_id=_ACTOR,
        roles=roles if roles is not None else ["producer"],
    )


def _make_ref() -> AdoptionEventRef:
    return AdoptionEventRef(
        adoption_id=_ADOPTION_ID,
        tenant_id=_TENANT,
        provider_capability_id=_CAP_ID,
        consumer_tenant_id=_CONSUMER_TENANT,
        actor_id=_ACTOR,
        intent="production",
        version_pin=None,
        t_valid_from=_NOW,
        t_valid_to=None,
        t_ingested_at=_NOW,
        t_invalidated_at=None,
    )


def _build_app(
    *,
    adopt_return: AdoptionEventRef | None = None,
    adopt_effect: Exception | None = None,
    active_return: AdoptionEventRef | None = None,
    ctx: TenantContext | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.include_router(mutation_router)

    adoption_svc = MagicMock()
    if adopt_effect is not None:
        adoption_svc.adopt = AsyncMock(side_effect=adopt_effect)
    else:
        adoption_svc.adopt = AsyncMock(return_value=adopt_return or _make_ref())
    adoption_svc.get_active_adoption = AsyncMock(return_value=active_return)
    adoption_svc.unadopt = AsyncMock(return_value=None)
    app.state.adoption = adoption_svc

    catalog_mock = MagicMock()

    async def _resolve(ctx_arg: TenantContext, handle: str, **_kw: object) -> EntityRef:
        return EntityRef(
            entity_id=uuid.UUID(handle),
            tenant_id=ctx_arg.tenant_id,
            entity_type="capability",
            name="cap",
            external_id=None,
            is_active=True,
            created_at=_NOW,
        )

    catalog_mock.resolve_entity_handle = _resolve
    app.state.catalog = catalog_mock

    from registry.api.middleware.tenant import get_tenant_context  # noqa: PLC0415

    effective_ctx = ctx if ctx is not None else _ctx()

    async def _fake_ctx() -> TenantContext:
        return effective_ctx

    app.dependency_overrides[get_tenant_context] = _fake_ctx
    return app


# ---------------------------------------------------------------------------
# POST /v1/capabilities/{cap_id}/adoptions
# ---------------------------------------------------------------------------


class TestAdoptCapability:
    def test_default_view_omits_bitemporal_fields(self) -> None:
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post(f"/v1/capabilities/{_CAP_ID}/adoptions", json={})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["adoption_id"] == str(_ADOPTION_ID)
        # Bitemporal fields must not appear in the default shape.
        assert "valid_from" not in body
        assert "valid_to" not in body
        assert "ingested_at" not in body
        assert "invalidated_at" not in body
        # t_* storage names must never appear.
        assert "t_valid_from" not in body
        assert "t_ingested_at" not in body

    def test_audit_view_populates_bitemporal_fields(self) -> None:
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post(f"/v1/capabilities/{_CAP_ID}/adoptions?view=audit", json={})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["adoption_id"] == str(_ADOPTION_ID)
        assert body["valid_from"] is not None
        assert body["ingested_at"] is not None
        assert body["valid_to"] is None
        assert body["invalidated_at"] is None

    def test_invalid_view_returns_422(self) -> None:
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(f"/v1/capabilities/{_CAP_ID}/adoptions?view=full", json={})
        assert resp.status_code == 422

    def test_not_found_returns_404(self) -> None:
        app = _build_app(adopt_effect=NotFoundError(f"cap {_CAP_ID} not found"))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(f"/v1/capabilities/{_CAP_ID}/adoptions", json={})
        assert resp.status_code == 404

    def test_validation_error_returns_422(self) -> None:
        app = _build_app(adopt_effect=ValidationError("bad intent"))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(f"/v1/capabilities/{_CAP_ID}/adoptions", json={"intent": "x" * 1000})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /v1/capabilities/{cap_id}/adoptions
# ---------------------------------------------------------------------------


class TestListAdoptions:
    def test_empty_returns_empty_list(self) -> None:
        app = _build_app(active_return=None)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/capabilities/{_CAP_ID}/adoptions")
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == []
        assert body["next_cursor"] is None

    def test_default_view_omits_bitemporal_fields(self) -> None:
        app = _build_app(active_return=_make_ref())
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/capabilities/{_CAP_ID}/adoptions")
        assert resp.status_code == 200
        body = resp.json()
        items = body["items"]
        assert len(items) == 1
        item = items[0]
        assert item["adoption_id"] == str(_ADOPTION_ID)
        assert "valid_from" not in item
        assert "ingested_at" not in item
        assert "t_valid_from" not in item

    def test_audit_view_populates_bitemporal_fields(self) -> None:
        app = _build_app(active_return=_make_ref())
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/capabilities/{_CAP_ID}/adoptions?view=audit")
        assert resp.status_code == 200
        body = resp.json()
        items = body["items"]
        assert len(items) == 1
        item = items[0]
        assert item["valid_from"] is not None
        assert item["ingested_at"] is not None

    def test_invalid_view_returns_422(self) -> None:
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(f"/v1/capabilities/{_CAP_ID}/adoptions?view=everything")
        assert resp.status_code == 422
