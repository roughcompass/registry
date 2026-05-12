"""Unit tests for ETag + If-Match rollout across all PATCH endpoints.

Coverage:
- PATCH /v1/capabilities/{id} (patch_capability) — stale If-Match → 412.
- PATCH /v1/capabilities/{id} — current If-Match → 200.
- PATCH /v1/capabilities/{id} — absent If-Match → 200 (advisory mode).
- GET /v1/concepts/{id} — emits ETag header.
- PATCH /v1/concepts/{id} — stale If-Match → 412.
- PATCH /v1/concepts/{id} — current If-Match → 200.
- PATCH /v1/concepts/{id} — absent If-Match → 200.
- GET /v1/operations/{id} — emits ETag header.
- PATCH /v1/operations/{id} — stale If-Match → 412.
- PATCH /v1/subscriptions/{id} — stale If-Match → 412.
- PATCH /v1/subscriptions/{id} — current If-Match → 200.
- PATCH /v1/subscriptions/{id} — absent If-Match → 200.

No database, no network — all service calls mocked via AsyncMock.
"""

from __future__ import annotations

import datetime
import uuid
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from registry.api.middleware.etag import compute_etag, latest_timestamp
from registry.types import TenantContext

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_TENANT = uuid.uuid4()
_ACTOR = uuid.uuid4()
_ENTITY_ID = uuid.uuid4()
_CAP_ID = uuid.uuid4()
_SUB_ID = uuid.uuid4()


def _ctx(roles: list[str] | None = None) -> TenantContext:
    return TenantContext(
        tenant_id=_TENANT,
        actor_id=_ACTOR,
        roles=roles if roles is not None else ["producer"],
    )


# ---------------------------------------------------------------------------
# Capability record mock factory
# ---------------------------------------------------------------------------


def _make_capability_record(entity_id: uuid.UUID = _ENTITY_ID) -> MagicMock:
    entity = MagicMock()
    entity.entity_id = entity_id
    entity.tenant_id = _TENANT
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


def _etag_for_record(record: MagicMock) -> str:
    """Compute the ETag a handler would produce for this record."""
    ts = latest_timestamp(
        record.entity.created_at,
        *(f.t_ingested_at for f in record.facts),
    )
    return compute_etag(record.entity.entity_id, ts)


# ---------------------------------------------------------------------------
# PATCH /v1/capabilities/{id} — patch_capability
# ---------------------------------------------------------------------------


class TestPatchCapabilityIfMatch:
    """patch_capability now honours If-Match."""

    def _build_app(self, *, ctx: TenantContext | None = None) -> tuple[FastAPI, MagicMock]:
        from registry.api.routers.capabilities import mutation_router, router  # noqa: PLC0415

        app = FastAPI()
        app.include_router(router)
        app.include_router(mutation_router)

        record = _make_capability_record()
        catalog_svc = MagicMock()
        catalog_svc.resolve_entity_handle = AsyncMock(return_value=record.entity)
        catalog_svc.get_full_capability = AsyncMock(return_value=record)
        catalog_svc.update_entity = AsyncMock(return_value=None)
        app.state.catalog = catalog_svc

        from registry.api.middleware.tenant import get_tenant_context  # noqa: PLC0415

        effective_ctx = ctx or _ctx()

        async def _fake_ctx() -> TenantContext:
            return effective_ctx

        app.dependency_overrides[get_tenant_context] = _fake_ctx
        return app, catalog_svc

    def test_stale_if_match_returns_412(self) -> None:
        app, _ = self._build_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.patch(
            f"/v1/capabilities/{_ENTITY_ID}",
            json={"updates": {"name": "NewName"}},
            headers={"If-Match": 'W/"staleetag"'},
        )
        assert resp.status_code == 412, resp.text

    def test_current_if_match_returns_200(self) -> None:
        app, catalog_svc = self._build_app()
        # Derive the real ETag from the mock record.
        record = _make_capability_record()
        catalog_svc.get_full_capability = AsyncMock(return_value=record)
        etag = _etag_for_record(record)

        client = TestClient(app, raise_server_exceptions=True)
        resp = client.patch(
            f"/v1/capabilities/{_ENTITY_ID}",
            json={"updates": {}},
            headers={"If-Match": etag},
        )
        assert resp.status_code == 200, resp.text

    def test_absent_if_match_returns_200(self) -> None:
        """Advisory mode: absent header → write proceeds."""
        app, _ = self._build_app()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.patch(
            f"/v1/capabilities/{_ENTITY_ID}",
            json={"updates": {}},
        )
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# GET /v1/concepts/{id} — ETag emission
# ---------------------------------------------------------------------------


class TestGetConceptETag:
    def _build_app(self) -> FastAPI:
        from registry.api.routers.concepts import router  # noqa: PLC0415

        app = FastAPI()
        app.include_router(router)

        record = _make_capability_record()
        catalog_svc = MagicMock()
        catalog_svc.resolve_entity_handle = AsyncMock(return_value=record.entity)
        catalog_svc.get_full_capability = AsyncMock(return_value=record)
        app.state.catalog = catalog_svc

        from registry.api.middleware.tenant import get_tenant_context  # noqa: PLC0415

        async def _fake_ctx() -> TenantContext:
            return _ctx()

        app.dependency_overrides[get_tenant_context] = _fake_ctx
        return app

    def test_get_concept_emits_etag_header(self) -> None:
        app = self._build_app()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/concepts/{_ENTITY_ID}")
        assert resp.status_code == 200, resp.text
        assert "ETag" in resp.headers
        etag = resp.headers["ETag"]
        assert etag.startswith('W/"'), f"ETag should be weak: {etag!r}"

    def test_get_concept_etag_matches_computed_value(self) -> None:
        app = self._build_app()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/concepts/{_ENTITY_ID}")
        assert resp.status_code == 200
        record = _make_capability_record()
        expected_etag = _etag_for_record(record)
        assert resp.headers["ETag"] == expected_etag


# ---------------------------------------------------------------------------
# PATCH /v1/concepts/{id} — If-Match
# ---------------------------------------------------------------------------


class TestPatchConceptIfMatch:
    def _build_app(self, *, ctx: TenantContext | None = None) -> tuple[FastAPI, MagicMock]:
        from registry.api.routers.concepts import mutation_router, router  # noqa: PLC0415

        app = FastAPI()
        app.include_router(router)
        app.include_router(mutation_router)

        record = _make_capability_record()
        catalog_svc = MagicMock()
        catalog_svc.resolve_entity_handle = AsyncMock(return_value=record.entity)
        catalog_svc.get_full_capability = AsyncMock(return_value=record)
        catalog_svc.update_entity = AsyncMock(return_value=None)
        app.state.catalog = catalog_svc

        from registry.api.middleware.tenant import get_tenant_context  # noqa: PLC0415

        effective_ctx = ctx or _ctx()

        async def _fake_ctx() -> TenantContext:
            return effective_ctx

        app.dependency_overrides[get_tenant_context] = _fake_ctx
        return app, catalog_svc

    def test_stale_if_match_returns_412(self) -> None:
        app, _ = self._build_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.patch(
            f"/v1/concepts/{_ENTITY_ID}",
            json={"updates": {}},
            headers={"If-Match": 'W/"staleetag"'},
        )
        assert resp.status_code == 412, resp.text

    def test_current_if_match_returns_200(self) -> None:
        app, catalog_svc = self._build_app()
        record = _make_capability_record()
        catalog_svc.get_full_capability = AsyncMock(return_value=record)
        etag = _etag_for_record(record)

        client = TestClient(app, raise_server_exceptions=True)
        resp = client.patch(
            f"/v1/concepts/{_ENTITY_ID}",
            json={"updates": {}},
            headers={"If-Match": etag},
        )
        assert resp.status_code == 200, resp.text

    def test_absent_if_match_returns_200(self) -> None:
        app, _ = self._build_app()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.patch(
            f"/v1/concepts/{_ENTITY_ID}",
            json={"updates": {}},
        )
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# GET /v1/operations/{id} — ETag emission
# ---------------------------------------------------------------------------


class TestGetOperationETag:
    def _build_app(self) -> FastAPI:
        from registry.api.routers.operations import router  # noqa: PLC0415

        app = FastAPI()
        app.include_router(router)

        record = _make_capability_record()
        catalog_svc = MagicMock()
        catalog_svc.resolve_entity_handle = AsyncMock(return_value=record.entity)
        catalog_svc.get_full_capability = AsyncMock(return_value=record)
        app.state.catalog = catalog_svc

        from registry.api.middleware.tenant import get_tenant_context  # noqa: PLC0415

        async def _fake_ctx() -> TenantContext:
            return _ctx()

        app.dependency_overrides[get_tenant_context] = _fake_ctx
        return app

    def test_get_operation_emits_etag_header(self) -> None:
        app = self._build_app()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/operations/{_ENTITY_ID}")
        assert resp.status_code == 200, resp.text
        assert "ETag" in resp.headers
        etag = resp.headers["ETag"]
        assert etag.startswith('W/"'), f"ETag should be weak: {etag!r}"


# ---------------------------------------------------------------------------
# PATCH /v1/operations/{id} — stale If-Match → 412
# ---------------------------------------------------------------------------


class TestPatchOperationIfMatch:
    def _build_app(self) -> FastAPI:
        from registry.api.routers.operations import mutation_router, router  # noqa: PLC0415

        app = FastAPI()
        app.include_router(router)
        app.include_router(mutation_router)

        record = _make_capability_record()
        catalog_svc = MagicMock()
        catalog_svc.resolve_entity_handle = AsyncMock(return_value=record.entity)
        catalog_svc.get_full_capability = AsyncMock(return_value=record)
        catalog_svc.update_entity = AsyncMock(return_value=None)
        app.state.catalog = catalog_svc

        from registry.api.middleware.tenant import get_tenant_context  # noqa: PLC0415

        async def _fake_ctx() -> TenantContext:
            return _ctx()

        app.dependency_overrides[get_tenant_context] = _fake_ctx
        return app

    def test_stale_if_match_returns_412(self) -> None:
        app = self._build_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.patch(
            f"/v1/operations/{_ENTITY_ID}",
            json={"updates": {}},
            headers={"If-Match": 'W/"stale"'},
        )
        assert resp.status_code == 412, resp.text

    def test_absent_if_match_returns_200(self) -> None:
        app = self._build_app()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.patch(
            f"/v1/operations/{_ENTITY_ID}",
            json={"updates": {}},
        )
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# PATCH /v1/subscriptions/{id} — If-Match
# ---------------------------------------------------------------------------


class TestPatchSubscriptionIfMatch:
    def _make_sub_ref(self) -> object:
        from registry.types import SubscriptionRef  # noqa: PLC0415

        return SubscriptionRef(
            subscription_id=_SUB_ID,
            tenant_id=_TENANT,
            actor_id=_ACTOR,
            capability_id=_CAP_ID,
            event_kinds=["version_published"],
            webhook_url=None,
            webhook_hmac_secret_ref=None,
            is_enabled=True,
            digest_window="none",
            t_valid_from=_NOW,
            t_valid_to=None,
            t_ingested_at=_NOW,
            t_invalidated_at=None,
        )

    def _build_app(self, *, list_return: list | None = None) -> tuple[FastAPI, MagicMock]:
        from registry.api.routers.subscriptions import mutation_router  # noqa: PLC0415

        app = FastAPI()
        app.include_router(mutation_router)

        sub_ref = self._make_sub_ref()
        svc = MagicMock()
        svc.list_subscriptions = AsyncMock(return_value=list_return if list_return is not None else [sub_ref])
        svc.update_subscription = AsyncMock(return_value=sub_ref)
        app.state.subscriptions = svc

        catalog_mock = MagicMock()
        app.state.catalog = catalog_mock

        from registry.api.middleware.tenant import get_tenant_context  # noqa: PLC0415

        async def _fake_ctx() -> TenantContext:
            return _ctx(roles=["consumer"])

        app.dependency_overrides[get_tenant_context] = _fake_ctx
        return app, svc

    def _compute_sub_etag(self) -> str:
        return compute_etag(_SUB_ID, latest_timestamp(_NOW))

    def test_stale_if_match_returns_412(self) -> None:
        app, _ = self._build_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.patch(
            f"/v1/subscriptions/{_SUB_ID}",
            json={"is_enabled": False},
            headers={"If-Match": 'W/"stale"'},
        )
        assert resp.status_code == 412, resp.text

    def test_current_if_match_returns_200(self) -> None:
        app, _ = self._build_app()
        etag = self._compute_sub_etag()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.patch(
            f"/v1/subscriptions/{_SUB_ID}",
            json={"is_enabled": False},
            headers={"If-Match": etag},
        )
        assert resp.status_code == 200, resp.text

    def test_absent_if_match_returns_200(self) -> None:
        app, _ = self._build_app()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.patch(
            f"/v1/subscriptions/{_SUB_ID}",
            json={"is_enabled": False},
        )
        assert resp.status_code == 200, resp.text

    def test_subscription_not_found_in_list_returns_404(self) -> None:
        """When list_subscriptions returns no match, PATCH should 404 before mutating."""
        app, _ = self._build_app(list_return=[])
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.patch(
            f"/v1/subscriptions/{_SUB_ID}",
            json={"is_enabled": False},
        )
        assert resp.status_code == 404, resp.text
