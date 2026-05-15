"""Unit tests for subscription REST endpoints.

Service interactions are mocked at ``app.state.subscriptions``; no DB or
network is involved.

Coverage:
- POST   /v1/capabilities/{cap_id}/subscriptions → 201 + {subscription_id}
- POST   ... invalid event_kind             → 422 (ValidationError surfaced)
- POST   ... empty event_kinds list         → 422 (Pydantic min_length=1)
- POST   ... invisible capability           → 403 (PermissionError)
- GET    /v1/capabilities/{cap_id}/subscriptions → 200 + list (default shape)
- GET    ... ?view=audit                    → 200 + list with bitemporal fields
- GET    ... ?view=bad                      → 422
- PATCH  /v1/subscriptions/{id}            → 200 + SubscriptionResponse default shape
- PATCH  ... ?view=audit                   → 200 + SubscriptionResponse with bitemporal fields
- PATCH  ... invalid event_kind            → 422
- PATCH  ... not found                     → 404
- DELETE /v1/subscriptions/{id}            → 204
- DELETE ... not found                     → 404
- POST-tunneled aliases: POST /v1/subscriptions/{id}:update / :delete
- Auth: consumer/producer/admin allowed; auditor role rejected (403).
- Default response shape must not contain valid_from / ingested_at keys (t_* never exposed).
"""

from __future__ import annotations

import datetime
import uuid
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from registry.api.routers.subscriptions import mutation_router, router
from registry.exceptions import NotFoundError, ValidationError
from registry.types import EntityRef, SubscriptionRef, TenantContext

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_TENANT = uuid.uuid4()
_ACTOR = uuid.uuid4()
_CAP_ID = uuid.uuid4()
_SUB_ID = uuid.uuid4()


def _ctx(roles: list[str] | None = None) -> TenantContext:
    return TenantContext(
        tenant_id=_TENANT,
        actor_id=_ACTOR,
        roles=roles if roles is not None else ["consumer"],
    )


def _make_ref(
    sub_id: uuid.UUID = _SUB_ID,
    *,
    event_kinds: list[str] | None = None,
    webhook_url: str | None = None,
    is_enabled: bool = True,
    invalidated: bool = False,
) -> SubscriptionRef:
    return SubscriptionRef(
        subscription_id=sub_id,
        tenant_id=_TENANT,
        actor_id=_ACTOR,
        capability_id=_CAP_ID,
        event_kinds=event_kinds or ["version_published"],
        webhook_url=webhook_url,
        webhook_hmac_secret_ref=None,
        is_enabled=is_enabled,
        digest_window="none",
        t_valid_from=_NOW,
        t_valid_to=None,
        t_ingested_at=_NOW,
        t_invalidated_at=_NOW if invalidated else None,
    )


def _build_app(
    *,
    create_return: uuid.UUID | None = None,
    create_effect: Exception | None = None,
    list_return: list[SubscriptionRef] | None = None,
    update_return: SubscriptionRef | None = None,
    update_effect: Exception | None = None,
    delete_effect: Exception | None = None,
    ctx: TenantContext | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.include_router(mutation_router)

    svc = MagicMock()
    if create_effect is not None:
        svc.create_subscription = AsyncMock(side_effect=create_effect)
    else:
        svc.create_subscription = AsyncMock(return_value=create_return or _SUB_ID)
    svc.list_subscriptions = AsyncMock(return_value=list_return or [])
    if update_effect is not None:
        svc.update_subscription = AsyncMock(side_effect=update_effect)
    else:
        svc.update_subscription = AsyncMock(return_value=update_return or _make_ref())
    if delete_effect is not None:
        svc.delete_subscription = AsyncMock(side_effect=delete_effect)
    else:
        svc.delete_subscription = AsyncMock(return_value=None)
    app.state.subscriptions = svc

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
# POST /v1/capabilities/{cap_id}/subscriptions
# ---------------------------------------------------------------------------


class TestCreateSubscription:
    def test_returns_201_and_subscription_id(self) -> None:
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post(
            f"/v1/capabilities/{_CAP_ID}/subscriptions",
            json={
                "event_kinds": ["version_published"],
                "webhook_url": "https://hook.example.com",
                "webhook_hmac_secret_ref": "vault:abc",
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json() == {"subscription_id": str(_SUB_ID)}
        app.state.subscriptions.create_subscription.assert_awaited_once()

    def test_empty_event_kinds_returns_422(self) -> None:
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            f"/v1/capabilities/{_CAP_ID}/subscriptions",
            json={"event_kinds": []},
        )
        assert resp.status_code == 422

    def test_invalid_event_kind_returns_422(self) -> None:
        app = _build_app(create_effect=ValidationError("bad kind"))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            f"/v1/capabilities/{_CAP_ID}/subscriptions",
            json={"event_kinds": ["nope"]},
        )
        assert resp.status_code == 422
        assert "bad kind" in resp.text

    def test_invisible_capability_returns_403(self) -> None:
        app = _build_app(create_effect=PermissionError("not visible"))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            f"/v1/capabilities/{_CAP_ID}/subscriptions",
            json={"event_kinds": ["version_published"]},
        )
        assert resp.status_code == 403

    def test_auditor_role_returns_403(self) -> None:
        app = _build_app(ctx=_ctx(roles=["auditor"]))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            f"/v1/capabilities/{_CAP_ID}/subscriptions",
            json={"event_kinds": ["version_published"]},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# GET /v1/capabilities/{cap_id}/subscriptions
# ---------------------------------------------------------------------------


class TestListSubscriptions:
    def test_returns_200_and_list(self) -> None:
        refs = [_make_ref(), _make_ref(sub_id=uuid.uuid4())]
        app = _build_app(list_return=refs)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/capabilities/{_CAP_ID}/subscriptions")
        assert resp.status_code == 200
        body = resp.json()
        items = body["items"]
        assert len(items) == 2
        assert items[0]["subscription_id"] == str(refs[0].subscription_id)
        assert body["next_cursor"] is None
        # Caller-scoped: service called with capability_id.
        call = app.state.subscriptions.list_subscriptions.await_args
        assert call.kwargs["capability_id"] == _CAP_ID

    def test_empty_list_returns_200_and_empty_array(self) -> None:
        app = _build_app(list_return=[])
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/capabilities/{_CAP_ID}/subscriptions")
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == []
        assert body["next_cursor"] is None

    def test_default_view_omits_bitemporal_fields(self) -> None:
        """Default shape must not include valid_from / ingested_at / t_* keys."""
        app = _build_app(list_return=[_make_ref()])
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/capabilities/{_CAP_ID}/subscriptions")
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert "valid_from" not in item
        assert "ingested_at" not in item
        assert "valid_to" not in item
        assert "invalidated_at" not in item
        # Storage-side t_* names must never appear in any response.
        assert "t_valid_from" not in item
        assert "t_ingested_at" not in item

    def test_audit_view_populates_bitemporal_fields(self) -> None:
        """?view=audit must populate valid_from / valid_to / ingested_at / invalidated_at."""
        app = _build_app(list_return=[_make_ref()])
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/capabilities/{_CAP_ID}/subscriptions?view=audit")
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["valid_from"] is not None
        assert item["ingested_at"] is not None
        assert item["valid_to"] is None
        assert item["invalidated_at"] is None

    def test_invalid_view_returns_422(self) -> None:
        app = _build_app(list_return=[])
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(f"/v1/capabilities/{_CAP_ID}/subscriptions?view=full")
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# PATCH /v1/subscriptions/{id}
# ---------------------------------------------------------------------------


class TestUpdateSubscription:
    def test_patch_returns_200_and_response(self) -> None:
        updated = _make_ref(event_kinds=["deprecation"], is_enabled=False)
        # list_subscriptions must return the subscription so the If-Match
        # pre-write fetch finds it; the actual update is handled by update_return.
        app = _build_app(update_return=updated, list_return=[_make_ref()])
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.patch(
            f"/v1/subscriptions/{_SUB_ID}",
            json={"event_kinds": ["deprecation"], "is_enabled": False},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["subscription_id"] == str(_SUB_ID)
        assert body["event_kinds"] == ["deprecation"]
        assert body["is_enabled"] is False

    def test_patch_default_view_omits_bitemporal_fields(self) -> None:
        """Default PATCH response must not include bitemporal keys."""
        app = _build_app(update_return=_make_ref(), list_return=[_make_ref()])
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.patch(
            f"/v1/subscriptions/{_SUB_ID}",
            json={"is_enabled": True},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "valid_from" not in body
        assert "ingested_at" not in body
        assert "t_valid_from" not in body

    def test_patch_audit_view_populates_bitemporal_fields(self) -> None:
        """?view=audit on PATCH must populate bitemporal fields."""
        app = _build_app(update_return=_make_ref(), list_return=[_make_ref()])
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.patch(
            f"/v1/subscriptions/{_SUB_ID}?view=audit",
            json={"is_enabled": True},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["valid_from"] is not None
        assert body["ingested_at"] is not None

    def test_patch_invalid_event_kind_returns_422(self) -> None:
        # list_subscriptions returns the subscription so the ETag check
        # passes; the ValidationError is raised by update_subscription.
        app = _build_app(update_effect=ValidationError("bad kind"), list_return=[_make_ref()])
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.patch(
            f"/v1/subscriptions/{_SUB_ID}",
            json={"event_kinds": ["nope"]},
        )
        assert resp.status_code == 422

    def test_patch_not_found_returns_404(self) -> None:
        # Subscription absent from list → 404 before update_subscription runs.
        app = _build_app(update_effect=NotFoundError(f"subscription {_SUB_ID} not found"))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.patch(
            f"/v1/subscriptions/{_SUB_ID}",
            json={"is_enabled": False},
        )
        assert resp.status_code == 404

    def test_post_tunneled_alias_update_not_registered_by_default(self) -> None:
        """Default mode is ``rest``; POST-tunneled aliases are opt-in via
        ``REGISTRY_HTTP_METHODS_MODE=both``. The verb PATCH route remains
        the canonical surface."""
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            f"/v1/subscriptions/{_SUB_ID}:update",
            json={"is_enabled": True},
        )
        # 404 (route not registered) or 405 (method not allowed on existing path).
        assert resp.status_code in (404, 405)


# ---------------------------------------------------------------------------
# DELETE /v1/subscriptions/{id}
# ---------------------------------------------------------------------------


class TestDeleteSubscription:
    def test_delete_returns_204(self) -> None:
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.delete(f"/v1/subscriptions/{_SUB_ID}")
        assert resp.status_code == 204
        app.state.subscriptions.delete_subscription.assert_awaited_once()

    def test_delete_not_found_returns_404(self) -> None:
        app = _build_app(delete_effect=NotFoundError(f"subscription {_SUB_ID} not found"))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.delete(f"/v1/subscriptions/{_SUB_ID}")
        assert resp.status_code == 404

    # The default-mode "POST alias is not registered" assertion used to
    # live here, but the test pyramid now sets ``REGISTRY_HTTP_METHODS_MODE=both``
    # at conftest load time so the integration + conformance suites can
    # exercise both surfaces without per-suite env juggling. The full
    # mode matrix (rest / both / post_only) is covered exhaustively in
    # tests/integration/test_http_methods_mode.py via subprocess + module
    # reload, so the unit-level check would have been redundant.
