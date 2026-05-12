"""Unit tests verifying that detail responses carry ``_links.self`` (and
resource-specific pointers where applicable).

Every detail-GET handler updated in CON-T05 is exercised here. The
assertions are kept narrow: presence + shape of ``_links``, not full
response body coverage (that lives in the resource-specific test files).
"""

from __future__ import annotations

import datetime
import uuid
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_TENANT = uuid.uuid4()
_ACTOR = uuid.uuid4()
_CAP_ID = uuid.uuid4()
_ENTITY_ID = uuid.uuid4()
_SUB_ID = uuid.uuid4()
_ADOPTION_ID = uuid.uuid4()


# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------


def _ctx(roles: list[str] | None = None):
    from registry.types import TenantContext  # noqa: PLC0415

    return TenantContext(
        tenant_id=_TENANT,
        actor_id=_ACTOR,
        roles=roles if roles is not None else ["producer"],
    )


def _entity_ref(entity_id: uuid.UUID):
    from registry.types import EntityRef  # noqa: PLC0415

    return EntityRef(
        entity_id=entity_id,
        tenant_id=_TENANT,
        entity_type="capability",
        name="my-cap",
        external_id=None,
        is_active=True,
        created_at=_NOW,
    )


def _capability_record(entity_id: uuid.UUID):
    """Minimal CapabilityRecord mock."""
    rec = MagicMock()
    rec.entity = _entity_ref(entity_id)
    rec.entity.entity_id = entity_id
    rec.entity.tenant_id = _TENANT
    rec.entity.name = "my-cap"
    rec.entity.external_id = None
    rec.entity.lifecycle = "active"
    rec.entity.is_active = True
    rec.entity.created_at = _NOW
    rec.entity.entity_type = "capability"
    rec.lifecycle = "active"
    rec.attributes = {}
    rec.facts = []
    rec.edges_out = []
    rec.edges_in = []
    rec.superseded_facts_count = 0
    return rec


# ---------------------------------------------------------------------------
# Concepts — GET /v1/concepts/{entity_id} must carry _links.self
# ---------------------------------------------------------------------------


def _build_concept_app() -> FastAPI:
    from registry.api.middleware.tenant import get_tenant_context  # noqa: PLC0415
    from registry.api.routers.concepts import mutation_router, router  # noqa: PLC0415

    app = FastAPI()
    app.include_router(router)
    app.include_router(mutation_router)

    record = _capability_record(_ENTITY_ID)

    catalog_svc = MagicMock()
    catalog_svc.resolve_entity_handle = AsyncMock(return_value=_entity_ref(_ENTITY_ID))
    catalog_svc.get_full_capability = AsyncMock(return_value=record)
    app.state.catalog = catalog_svc

    async def _fake_ctx():
        return _ctx()

    app.dependency_overrides[get_tenant_context] = _fake_ctx
    return app


class TestConceptDetailLinks:
    def test_get_returns_self_link(self) -> None:
        app = _build_concept_app()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/concepts/{_ENTITY_ID}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "_links" in body
        assert body["_links"]["self"] == f"/v1/concepts/{_ENTITY_ID}"

    def test_post_response_has_no_links(self) -> None:
        """POST (create) returns CapabilityResponse which has no _links — the
        detail-GET shape (EntityDetailResponse) is what carries _links."""
        from registry.api.middleware.tenant import get_tenant_context  # noqa: PLC0415
        from registry.api.routers.concepts import mutation_router, router  # noqa: PLC0415

        record = _capability_record(_ENTITY_ID)

        app = FastAPI()
        app.include_router(router)
        app.include_router(mutation_router)

        catalog_svc = MagicMock()
        catalog_svc.resolve_entity_handle = AsyncMock(return_value=_entity_ref(_ENTITY_ID))
        catalog_svc.create_entity = AsyncMock(return_value=_entity_ref(_ENTITY_ID))
        catalog_svc.create_edge = AsyncMock(return_value=None)
        catalog_svc.get_full_capability = AsyncMock(return_value=record)
        app.state.catalog = catalog_svc

        async def _fake_ctx():
            return _ctx()

        app.dependency_overrides[get_tenant_context] = _fake_ctx

        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post(
            "/v1/concepts",
            json={"name": "my-concept", "entity_type": "concept"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert "_links" not in body


# ---------------------------------------------------------------------------
# Operations — GET /v1/operations/{entity_id} must carry _links.self
# ---------------------------------------------------------------------------


def _build_operation_app() -> FastAPI:
    from registry.api.middleware.tenant import get_tenant_context  # noqa: PLC0415
    from registry.api.routers.operations import mutation_router, router  # noqa: PLC0415

    app = FastAPI()
    app.include_router(router)
    app.include_router(mutation_router)

    record = _capability_record(_ENTITY_ID)

    catalog_svc = MagicMock()
    catalog_svc.resolve_entity_handle = AsyncMock(return_value=_entity_ref(_ENTITY_ID))
    catalog_svc.get_full_capability = AsyncMock(return_value=record)
    app.state.catalog = catalog_svc

    async def _fake_ctx():
        return _ctx()

    app.dependency_overrides[get_tenant_context] = _fake_ctx
    return app


class TestOperationDetailLinks:
    def test_get_returns_self_link(self) -> None:
        app = _build_operation_app()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/operations/{_ENTITY_ID}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "_links" in body
        assert body["_links"]["self"] == f"/v1/operations/{_ENTITY_ID}"


# ---------------------------------------------------------------------------
# Interface — GET /v1/capabilities/{id}/interface carries _links.self + capability
# ---------------------------------------------------------------------------


def _build_interface_app() -> FastAPI:
    from registry.api.middleware.tenant import get_tenant_context  # noqa: PLC0415
    from registry.api.routers.interface import router as interface_router  # noqa: PLC0415
    from registry.service.interface_storage import InterfaceRecord  # noqa: PLC0415

    app = FastAPI()
    app.include_router(interface_router)

    record = InterfaceRecord(
        capability_id=_CAP_ID,
        interface_canonical=None,
        interface_source=None,
        interface_format=None,
        as_of=None,
    )
    svc = MagicMock()
    svc.get_interface = AsyncMock(return_value=record)
    app.state.interface_storage = svc

    catalog_mock = MagicMock()
    catalog_mock.resolve_entity_handle = AsyncMock(return_value=_entity_ref(_CAP_ID))
    app.state.catalog = catalog_mock

    async def _fake_ctx():
        return _ctx()

    app.dependency_overrides[get_tenant_context] = _fake_ctx
    return app


class TestInterfaceDetailLinks:
    def test_get_returns_self_and_capability_links(self) -> None:
        app = _build_interface_app()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/capabilities/{_CAP_ID}/interface")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "_links" in body
        assert body["_links"]["self"] == f"/v1/capabilities/{_CAP_ID}/interface"
        assert body["_links"]["capability"] == f"/v1/capabilities/{_CAP_ID}"


# ---------------------------------------------------------------------------
# Subscriptions — PATCH /v1/subscriptions/{id} response carries _links
# ---------------------------------------------------------------------------


def _make_sub_ref():
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


def _build_subscription_app() -> FastAPI:
    from registry.api.middleware.tenant import get_tenant_context  # noqa: PLC0415
    from registry.api.routers.subscriptions import mutation_router, router  # noqa: PLC0415

    app = FastAPI()
    app.include_router(router)
    app.include_router(mutation_router)

    svc = MagicMock()
    svc.create_subscription = AsyncMock(return_value=_SUB_ID)
    svc.list_subscriptions = AsyncMock(return_value=[_make_sub_ref()])
    svc.update_subscription = AsyncMock(return_value=_make_sub_ref())
    svc.delete_subscription = AsyncMock(return_value=None)
    app.state.subscriptions = svc

    catalog_mock = MagicMock()
    catalog_mock.resolve_entity_handle = AsyncMock(return_value=_entity_ref(_CAP_ID))
    app.state.catalog = catalog_mock

    async def _fake_ctx():
        return _ctx(roles=["consumer"])

    app.dependency_overrides[get_tenant_context] = _fake_ctx
    return app


class TestSubscriptionLinks:
    def test_patch_response_has_self_and_capability_links(self) -> None:
        app = _build_subscription_app()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.patch(f"/v1/subscriptions/{_SUB_ID}", json={"is_enabled": True})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "_links" in body
        assert body["_links"]["self"] == f"/v1/subscriptions/{_SUB_ID}"
        assert body["_links"]["capability"] == f"/v1/capabilities/{_CAP_ID}"

    def test_list_response_items_have_no_links(self) -> None:
        """List endpoint items must not carry _links (per task constraint)."""
        app = _build_subscription_app()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/capabilities/{_CAP_ID}/subscriptions")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        items = body["items"]
        assert len(items) == 1
        assert "_links" not in items[0]


# ---------------------------------------------------------------------------
# Adoptions — POST /v1/capabilities/{cap}/adoptions response carries _links
# ---------------------------------------------------------------------------


def _make_adoption_ref():
    from registry.types import AdoptionEventRef  # noqa: PLC0415

    return AdoptionEventRef(
        adoption_id=_ADOPTION_ID,
        tenant_id=_TENANT,
        provider_capability_id=_CAP_ID,
        consumer_tenant_id=_TENANT,
        actor_id=_ACTOR,
        intent=None,
        version_pin=None,
        t_valid_from=_NOW,
        t_valid_to=None,
        t_ingested_at=_NOW,
        t_invalidated_at=None,
    )


def _build_adoption_app() -> FastAPI:
    from registry.api.middleware.tenant import get_tenant_context  # noqa: PLC0415
    from registry.api.routers.adoptions import mutation_router, router  # noqa: PLC0415

    app = FastAPI()
    app.include_router(router)
    app.include_router(mutation_router)

    svc = MagicMock()
    svc.adopt = AsyncMock(return_value=_make_adoption_ref())
    svc.get_active_adoption = AsyncMock(return_value=_make_adoption_ref())
    svc.unadopt = AsyncMock(return_value=None)
    app.state.adoption = svc

    catalog_mock = MagicMock()
    catalog_mock.resolve_entity_handle = AsyncMock(return_value=_entity_ref(_CAP_ID))
    app.state.catalog = catalog_mock

    async def _fake_ctx():
        return _ctx(roles=["producer"])

    app.dependency_overrides[get_tenant_context] = _fake_ctx
    return app


class TestAdoptionLinks:
    def test_post_response_has_self_and_capability_links(self) -> None:
        app = _build_adoption_app()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post(f"/v1/capabilities/{_CAP_ID}/adoptions", json={})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert "_links" in body
        assert body["_links"]["self"] == f"/v1/capabilities/{_CAP_ID}/adoptions/{_ADOPTION_ID}"
        assert body["_links"]["capability"] == f"/v1/capabilities/{_CAP_ID}"

    def test_list_response_items_have_no_links(self) -> None:
        """List endpoint items must not carry _links."""
        app = _build_adoption_app()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/capabilities/{_CAP_ID}/adoptions")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        items = body["items"]
        assert len(items) == 1
        assert "_links" not in items[0]
