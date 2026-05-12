"""Unit tests for the interface storage REST endpoints.

Service interactions mocked via AsyncMock; no DB or network involved.

Coverage:
- PUT happy path → 200 + InterfaceSurfaceResponse.
- PUT non-producer role → 403 (require_roles gate).
- PUT malformed format → 422 (ValidationError surfaced from service).
- PUT non-owner / missing capability → 404.
- GET current-truth happy path.
- GET as_of malformed → 422.
- GET no interface yet → 200 with null canonical.
- GET ?view=default (default) → 200 without audit fields.
- GET ?view=audit  → 200 accepted (no-op for interface; no extra fields surfaced).
- GET ?view=bad    → 422.
"""

from __future__ import annotations

import datetime
import uuid
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from registry.api.routers.interface import router as interface_router
from registry.exceptions import NotFoundError, ValidationError
from registry.service.interface_storage import InterfaceRecord
from registry.types import EntityRef, InterfaceSurface, TenantContext

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_TENANT = uuid.uuid4()
_ACTOR = uuid.uuid4()
_CAP = uuid.uuid4()


def _ctx(roles: list[str] | None = None) -> TenantContext:
    return TenantContext(
        tenant_id=_TENANT,
        actor_id=_ACTOR,
        roles=roles if roles is not None else ["producer"],
    )


def _build_app(
    *,
    put_return: InterfaceSurface | None = None,
    put_effect: Exception | None = None,
    get_return: InterfaceRecord | None = None,
    get_effect: Exception | None = None,
    ctx: TenantContext | None = None,
) -> FastAPI:
    import datetime  # noqa: PLC0415

    app = FastAPI()
    app.include_router(interface_router)

    svc = MagicMock()
    if put_effect is not None:
        svc.put_interface = AsyncMock(side_effect=put_effect)
    else:
        svc.put_interface = AsyncMock(return_value=put_return or InterfaceSurface(operations=[], events=[], fields=[]))
    if get_effect is not None:
        svc.get_interface = AsyncMock(side_effect=get_effect)
    else:
        svc.get_interface = AsyncMock(
            return_value=get_return
            or InterfaceRecord(
                capability_id=_CAP,
                interface_canonical=None,
                interface_source=None,
                interface_format=None,
                as_of=None,
            )
        )
    app.state.interface_storage = svc

    # Catalog mock: resolve_entity_handle echoes the UUID from the path param.
    catalog_mock = MagicMock()

    async def _resolve(ctx_arg: TenantContext, handle: str, **_kw: object) -> EntityRef:
        return EntityRef(
            entity_id=uuid.UUID(handle),
            tenant_id=ctx_arg.tenant_id,
            entity_type="capability",
            name="cap",
            external_id=None,
            is_active=True,
            created_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        )

    catalog_mock.resolve_entity_handle = _resolve
    app.state.catalog = catalog_mock

    from registry.api.middleware.tenant import get_tenant_context  # noqa: PLC0415

    effective = ctx if ctx is not None else _ctx()

    async def _fake_ctx() -> TenantContext:
        return effective

    app.dependency_overrides[get_tenant_context] = _fake_ctx
    return app


# ---------------------------------------------------------------------------
# PUT
# ---------------------------------------------------------------------------


class TestPutInterface:
    def test_happy_path_returns_canonical_surface(self) -> None:
        canonical = InterfaceSurface(
            operations=[],
            events=[],
            fields=[{"name": "id", "type": "string", "required": True}],
        )
        app = _build_app(put_return=canonical)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.put(
            f"/v1/capabilities/{_CAP}/interface",
            json={
                "interface_source": "type X = { id: string; }",
                "interface_format": "typescript",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["fields"] == [{"name": "id", "type": "string", "required": True}]

    def test_non_producer_role_returns_403(self) -> None:
        app = _build_app(ctx=_ctx(roles=["consumer"]))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.put(
            f"/v1/capabilities/{_CAP}/interface",
            json={"interface_source": {}, "interface_format": "json_schema"},
        )
        assert resp.status_code == 403

    def test_malformed_format_returns_422(self) -> None:
        app = _build_app(put_effect=ValidationError("bad format"))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.put(
            f"/v1/capabilities/{_CAP}/interface",
            json={"interface_source": {}, "interface_format": "graphql"},
        )
        assert resp.status_code == 422

    def test_not_found_returns_404(self) -> None:
        app = _build_app(put_effect=NotFoundError(f"{_CAP} not found"))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.put(
            f"/v1/capabilities/{_CAP}/interface",
            json={"interface_source": {}, "interface_format": "json_schema"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------


class TestGetInterface:
    def test_no_interface_returns_200_with_nulls(self) -> None:
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/capabilities/{_CAP}/interface")
        assert resp.status_code == 200
        body = resp.json()
        assert body["interface_canonical"] is None
        assert body["interface_source"] is None
        assert body["interface_format"] is None
        assert body["as_of"] is None

    def test_current_truth_returns_canonical(self) -> None:
        canonical = InterfaceSurface(
            operations=[{"name": "ping", "method": "GET", "path": "/ping", "params": [], "returns": "object"}],
            events=[],
            fields=[],
        )
        record = InterfaceRecord(
            capability_id=_CAP,
            interface_canonical=canonical,
            interface_source={"format": "openapi", "raw": {"openapi": "3.0.0"}},
            interface_format="openapi",
            as_of=None,
        )
        app = _build_app(get_return=record)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/capabilities/{_CAP}/interface")
        assert resp.status_code == 200
        body = resp.json()
        assert body["interface_format"] == "openapi"
        assert body["interface_canonical"]["operations"][0]["name"] == "ping"

    def test_malformed_as_of_returns_422(self) -> None:
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            f"/v1/capabilities/{_CAP}/interface",
            params={"as_of": "not-a-date"},
        )
        assert resp.status_code == 422

    def test_naive_as_of_returns_422(self) -> None:
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            f"/v1/capabilities/{_CAP}/interface",
            params={"as_of": "2026-01-01T00:00:00"},  # no timezone
        )
        assert resp.status_code == 422

    def test_view_audit_accepted_as_no_op(self) -> None:
        """?view=audit is a no-op for the interface endpoint (composed record,
        no individual bitemporal rows exposed). Must return 200 without error."""
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            f"/v1/capabilities/{_CAP}/interface",
            params={"view": "audit"},
        )
        assert resp.status_code == 200

    def test_invalid_view_returns_422(self) -> None:
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            f"/v1/capabilities/{_CAP}/interface",
            params={"view": "raw"},
        )
        assert resp.status_code == 422

    def test_default_view_omits_audit_fields(self) -> None:
        """Default shape must not contain valid_from / ingested_at keys."""
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/capabilities/{_CAP}/interface")
        assert resp.status_code == 200
        body = resp.json()
        assert "valid_from" not in body
        assert "ingested_at" not in body
        assert "t_valid_from" not in body
