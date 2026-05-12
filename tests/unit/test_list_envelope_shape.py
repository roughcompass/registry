"""Unit tests asserting the normalised list-response envelope shape.

Every list endpoint must return ``{items: list[T], next_cursor: str | null}``.
Tests here verify that:
- Previously-bare-list endpoints now wrap in the envelope.
- The audit endpoint uses ``items``, not ``rows``.
- SearchResponse uses ``items``.
- ``next_cursor`` is present (and ``None`` for bounded result sets).

No database involved — service calls are mocked.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_TENANT = uuid.uuid4()
_ACTOR = uuid.uuid4()
_CAP_ID = uuid.uuid4()


def _make_tenant_ctx(roles: list[str] | None = None) -> Any:
    from registry.types import TenantContext  # noqa: PLC0415

    return TenantContext(
        tenant_id=_TENANT,
        actor_id=_ACTOR,
        roles=roles if roles is not None else ["producer"],
    )


# ---------------------------------------------------------------------------
# SearchResponse — items field, total kept, no cursor
# ---------------------------------------------------------------------------


def test_search_response_uses_items_field() -> None:
    """SearchResponse must expose ``items``, not ``results``."""
    from registry.api.schemas import SearchResponse, SearchResultItem  # noqa: PLC0415

    item = SearchResultItem(
        entity_id=uuid.uuid4(),
        tenant_id=_TENANT,
        name="cap",
        entity_type="capability",
        score=0.9,
        retrieval_arms={},
        matching_facts=[],
    )
    resp = SearchResponse(items=[item], total=1, took_ms=5.0)
    assert len(resp.items) == 1
    assert resp.total == 1
    # ``results`` must not be a field on the model.
    assert not hasattr(resp, "results")


# ---------------------------------------------------------------------------
# AdoptionListResponse — envelope wraps items, next_cursor always None
# ---------------------------------------------------------------------------


def _make_adoption_ref() -> Any:
    from registry.types import AdoptionEventRef  # noqa: PLC0415

    return AdoptionEventRef(
        adoption_id=uuid.uuid4(),
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


def _build_adoption_app(*, active_return: Any = None) -> FastAPI:
    from registry.api.middleware.tenant import get_tenant_context  # noqa: PLC0415
    from registry.api.routers.adoptions import mutation_router, router  # noqa: PLC0415
    from registry.types import EntityRef  # noqa: PLC0415

    app = FastAPI()
    app.include_router(router)
    app.include_router(mutation_router)

    svc = MagicMock()
    svc.adopt = AsyncMock(return_value=_make_adoption_ref())
    svc.get_active_adoption = AsyncMock(return_value=active_return)
    svc.unadopt = AsyncMock(return_value=None)
    app.state.adoption = svc

    catalog_mock = MagicMock()
    catalog_mock.resolve_entity_handle = AsyncMock(
        return_value=EntityRef(
            entity_id=_CAP_ID,
            tenant_id=_TENANT,
            entity_type="capability",
            name="cap",
            external_id=None,
            is_active=True,
            created_at=_NOW,
        )
    )
    app.state.catalog = catalog_mock

    async def _fake_ctx() -> Any:
        return _make_tenant_ctx(roles=["producer"])

    app.dependency_overrides[get_tenant_context] = _fake_ctx
    return app


class TestAdoptionListEnvelope:
    def test_envelope_has_items_and_next_cursor(self) -> None:
        ref = _make_adoption_ref()
        app = _build_adoption_app(active_return=ref)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/capabilities/{_CAP_ID}/adoptions")
        assert resp.status_code == 200
        body = resp.json()
        assert "items" in body
        assert "next_cursor" in body
        assert body["next_cursor"] is None
        assert len(body["items"]) == 1

    def test_empty_returns_envelope_with_empty_items(self) -> None:
        app = _build_adoption_app(active_return=None)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/capabilities/{_CAP_ID}/adoptions")
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == []
        assert body["next_cursor"] is None

    def test_no_bare_list_at_top_level(self) -> None:
        """The response body must be an object, not a JSON array."""
        ref = _make_adoption_ref()
        app = _build_adoption_app(active_return=ref)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/capabilities/{_CAP_ID}/adoptions")
        assert resp.status_code == 200
        assert isinstance(resp.json(), dict), "response must be an object, not a bare list"


# ---------------------------------------------------------------------------
# SubscriptionListResponse — envelope wraps items, next_cursor always None
# ---------------------------------------------------------------------------


def _make_sub_ref(*, sub_id: uuid.UUID | None = None) -> Any:
    from registry.types import SubscriptionRef  # noqa: PLC0415

    return SubscriptionRef(
        subscription_id=sub_id or uuid.uuid4(),
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


def _build_subscription_app(*, list_return: list[Any] | None = None) -> FastAPI:
    from registry.api.middleware.tenant import get_tenant_context  # noqa: PLC0415
    from registry.api.routers.subscriptions import mutation_router, router  # noqa: PLC0415
    from registry.types import EntityRef  # noqa: PLC0415

    app = FastAPI()
    app.include_router(router)
    app.include_router(mutation_router)

    svc = MagicMock()
    svc.create_subscription = AsyncMock(return_value=uuid.uuid4())
    svc.list_subscriptions = AsyncMock(return_value=list_return or [])
    svc.update_subscription = AsyncMock(return_value=_make_sub_ref())
    svc.delete_subscription = AsyncMock(return_value=None)
    app.state.subscriptions = svc

    catalog_mock = MagicMock()
    catalog_mock.resolve_entity_handle = AsyncMock(
        return_value=EntityRef(
            entity_id=_CAP_ID,
            tenant_id=_TENANT,
            entity_type="capability",
            name="cap",
            external_id=None,
            is_active=True,
            created_at=_NOW,
        )
    )
    app.state.catalog = catalog_mock

    async def _fake_ctx() -> Any:
        return _make_tenant_ctx(roles=["consumer"])

    app.dependency_overrides[get_tenant_context] = _fake_ctx
    return app


class TestSubscriptionListEnvelope:
    def test_envelope_has_items_and_next_cursor(self) -> None:
        app = _build_subscription_app(list_return=[_make_sub_ref()])
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/capabilities/{_CAP_ID}/subscriptions")
        assert resp.status_code == 200
        body = resp.json()
        assert "items" in body
        assert "next_cursor" in body
        assert body["next_cursor"] is None
        assert len(body["items"]) == 1

    def test_empty_returns_envelope_with_empty_items(self) -> None:
        app = _build_subscription_app(list_return=[])
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/capabilities/{_CAP_ID}/subscriptions")
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == []
        assert body["next_cursor"] is None

    def test_no_bare_list_at_top_level(self) -> None:
        app = _build_subscription_app(list_return=[_make_sub_ref()])
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/capabilities/{_CAP_ID}/subscriptions")
        assert resp.status_code == 200
        assert isinstance(resp.json(), dict), "response must be an object, not a bare list"


# ---------------------------------------------------------------------------
# ExternalIdListResponse — envelope wraps items, next_cursor always None
# ---------------------------------------------------------------------------


def _make_ext_id_ref() -> Any:
    from registry.types import ExternalIdRef  # noqa: PLC0415

    return ExternalIdRef(
        external_id_pk=uuid.uuid4(),
        entity_id=uuid.uuid4(),
        tenant_id=_TENANT,
        external_system_slug="github",
        external_id="repo/123",
        url=None,
        metadata_jsonb=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _build_external_ids_app(*, list_return: list[Any] | None = None) -> FastAPI:
    from registry.api.middleware.tenant import get_tenant_context  # noqa: PLC0415
    from registry.api.routers.external_ids import entity_external_ids_router  # noqa: PLC0415
    from registry.types import EntityRef  # noqa: PLC0415

    entity_id = uuid.uuid4()

    app = FastAPI()
    app.include_router(entity_external_ids_router)

    svc = MagicMock()
    svc.list_external_ids = AsyncMock(return_value=list_return or [])
    app.state.external_ids = svc

    catalog_mock = MagicMock()
    catalog_mock.resolve_entity_handle = AsyncMock(
        return_value=EntityRef(
            entity_id=entity_id,
            tenant_id=_TENANT,
            entity_type="capability",
            name="cap",
            external_id=None,
            is_active=True,
            created_at=_NOW,
        )
    )
    app.state.catalog = catalog_mock

    async def _fake_ctx() -> Any:
        return _make_tenant_ctx(roles=["producer"])

    app.dependency_overrides[get_tenant_context] = _fake_ctx
    return app, entity_id


class TestExternalIdListEnvelope:
    def test_envelope_has_items_and_next_cursor(self) -> None:
        ref = _make_ext_id_ref()
        app, entity_id = _build_external_ids_app(list_return=[ref])
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/entities/{entity_id}/external-ids")
        assert resp.status_code == 200
        body = resp.json()
        assert "items" in body
        assert "next_cursor" in body
        assert body["next_cursor"] is None
        assert len(body["items"]) == 1

    def test_empty_returns_envelope_with_empty_items(self) -> None:
        app, entity_id = _build_external_ids_app(list_return=[])
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/entities/{entity_id}/external-ids")
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == []
        assert body["next_cursor"] is None

    def test_no_bare_list_at_top_level(self) -> None:
        ref = _make_ext_id_ref()
        app, entity_id = _build_external_ids_app(list_return=[ref])
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/entities/{entity_id}/external-ids")
        assert resp.status_code == 200
        assert isinstance(resp.json(), dict), "response must be an object, not a bare list"


# ---------------------------------------------------------------------------
# IntegrationListResponse — envelope wraps items, next_cursor always None
# ---------------------------------------------------------------------------


def _make_entity_ref() -> Any:
    from registry.types import EntityRef  # noqa: PLC0415

    return EntityRef(
        entity_id=uuid.uuid4(),
        tenant_id=_TENANT,
        entity_type="integration",
        name="int-1",
        external_id=None,
        is_active=True,
        created_at=_NOW,
    )


def _build_integrations_app(*, refs: list[Any] | None = None) -> FastAPI:
    from registry.api.middleware.tenant import get_tenant_context  # noqa: PLC0415
    from registry.api.routers.integrations import router  # noqa: PLC0415

    app = FastAPI()
    app.include_router(router)

    svc = MagicMock()
    svc.find_integrations_connecting = AsyncMock(return_value=refs or [])
    app.state.integrations = svc

    async def _fake_ctx() -> Any:
        return _make_tenant_ctx(roles=["consumer"])

    app.dependency_overrides[get_tenant_context] = _fake_ctx
    return app


class TestIntegrationListEnvelope:
    def test_envelope_has_items_and_next_cursor(self) -> None:
        ref = _make_entity_ref()
        app = _build_integrations_app(refs=[ref])
        client = TestClient(app, raise_server_exceptions=True)
        cap_a = uuid.uuid4()
        cap_b = uuid.uuid4()
        resp = client.get(f"/v1/integrations?connects={cap_a}&and={cap_b}")
        assert resp.status_code == 200
        body = resp.json()
        assert "items" in body
        assert "next_cursor" in body
        assert body["next_cursor"] is None
        assert len(body["items"]) == 1

    def test_empty_returns_envelope_with_empty_items(self) -> None:
        app = _build_integrations_app(refs=[])
        client = TestClient(app, raise_server_exceptions=True)
        cap_a = uuid.uuid4()
        cap_b = uuid.uuid4()
        resp = client.get(f"/v1/integrations?connects={cap_a}&and={cap_b}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == []
        assert body["next_cursor"] is None

    def test_no_bare_list_at_top_level(self) -> None:
        ref = _make_entity_ref()
        app = _build_integrations_app(refs=[ref])
        client = TestClient(app, raise_server_exceptions=True)
        cap_a = uuid.uuid4()
        cap_b = uuid.uuid4()
        resp = client.get(f"/v1/integrations?connects={cap_a}&and={cap_b}")
        assert resp.status_code == 200
        assert isinstance(resp.json(), dict), "response must be an object, not a bare list"


# ---------------------------------------------------------------------------
# AuditResponse — items (not rows), next_cursor present
# ---------------------------------------------------------------------------


def test_audit_response_uses_items_not_rows() -> None:
    """AuditResponse must expose ``items``, not ``rows``."""
    from registry.api.routers.admin import AuditResponse, AuditRow  # noqa: PLC0415

    row = AuditRow(
        audit_id=uuid.uuid4(),
        actor_id=_ACTOR,
        action="create",
        target_type="entity",
        target_id=uuid.uuid4(),
        before_jsonb=None,
        after_jsonb=None,
        ts=_NOW,
        request_id=None,
        error_code=None,
    )
    resp = AuditResponse(items=[row], next_cursor=None)
    assert len(resp.items) == 1
    assert resp.next_cursor is None
    # ``rows`` must not be a field on the model.
    assert not hasattr(resp, "rows")
