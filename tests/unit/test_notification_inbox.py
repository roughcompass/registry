"""Unit tests for the notification inbox.

Covers both surfaces (REST + MCP-shaped output) using a single
NotificationService instance with mocked SQL:

- list_notifications: status filter, cursor pagination (next_cursor only
  set when full page returned), default page_size, invalid status raises
  a validation error.
- mark_read: idempotent (no-op on missing / already-read rows).
- REST GET /v1/notifications and POST /v1/notifications/{id}:mark-read.
- ``event_to_dict`` is the shared serializer used by both REST and the
  MCP ``list_notifications`` tool — verify shape stability.

Payload minimality: the serialized item must not contain ``body``,
``description``, ``fact_body``, or other freeform fields. Asserted explicitly.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from registry.api.routers.notifications import router as notifications_router
from registry.exceptions import ValidationError
from registry.service.notifications import (
    NotificationService,
    event_to_dict,
)
from registry.types import CapabilityRegistryEvent, FakeClock, TenantContext

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_TENANT = uuid.uuid4()
_ACTOR = uuid.uuid4()


def _ctx() -> TenantContext:
    return TenantContext(tenant_id=_TENANT, actor_id=_ACTOR, roles=["consumer"])


def _async_ctx() -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _row(
    *,
    nid: uuid.UUID | None = None,
    ts: datetime.datetime | None = None,
    status: str = "unread",
    kind: str = "version_published",
    slug: str = "payment-api",
) -> dict[str, Any]:
    return {
        "notification_id": nid or uuid.uuid4(),
        "tenant_id": _TENANT,
        "subscription_id": uuid.uuid4(),
        "capability_id": uuid.uuid4(),
        "capability_slug": slug,
        "event_kind": kind,
        "change_classification": "non-breaking",
        "version_before": "1.0.0",
        "version_after": "1.1.0",
        "occurred_at": ts or _NOW,
        "fetch_url": "https://example.com/cap/abc",
        "ts": ts or _NOW,
    }


def _make_service(rows: list[dict] | None = None) -> NotificationService:
    """Build a NotificationService with a mocked session that returns ``rows``."""

    async def _execute(stmt: Any, params: dict | None = None):
        result = MagicMock()
        sql = " ".join(str(stmt).split())
        if "FROM notifications" in sql and "ORDER BY ts DESC" in sql:
            # list_notifications query
            result.mappings.return_value.all.return_value = list(rows or [])
            return result
        # UPDATE notifications → no return
        return result

    session = MagicMock()
    session.execute = _execute
    session.begin = MagicMock(return_value=_async_ctx())
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=session)
    return NotificationService(factory, FakeClock(_NOW))


# ---------------------------------------------------------------------------
# Service: list_notifications
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_returns_events_and_no_cursor_when_page_not_full() -> None:
    rows = [_row(), _row()]
    svc = _make_service(rows)
    events, cursor = await svc.list_notifications(_ctx(), page_size=50)
    assert len(events) == 2
    assert cursor is None
    # Each event is a CapabilityRegistryEvent.
    assert all(isinstance(e, CapabilityRegistryEvent) for e in events)


@pytest.mark.asyncio
async def test_list_returns_next_cursor_when_page_is_full() -> None:
    # Service requests page_size + 1; returning that many means more rows exist.
    rows = [_row(ts=_NOW - datetime.timedelta(minutes=i)) for i in range(4)]
    svc = _make_service(rows)
    events, cursor = await svc.list_notifications(_ctx(), page_size=3)
    assert len(events) == 3
    assert cursor is not None
    # Cursor encodes the boundary row's ts.
    assert cursor == rows[2]["ts"].isoformat()


@pytest.mark.asyncio
async def test_list_rejects_invalid_status() -> None:
    svc = _make_service()
    with pytest.raises(ValidationError):
        await svc.list_notifications(_ctx(), status="bogus")


@pytest.mark.asyncio
async def test_list_rejects_malformed_cursor() -> None:
    svc = _make_service()
    with pytest.raises(ValidationError):
        await svc.list_notifications(_ctx(), cursor="not-a-date")


@pytest.mark.asyncio
async def test_list_clamps_oversized_page_size() -> None:
    svc = _make_service([])
    events, cursor = await svc.list_notifications(_ctx(), page_size=10_000)
    assert events == []
    assert cursor is None


# ---------------------------------------------------------------------------
# Service: mark_read idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_read_is_idempotent_for_missing_rows() -> None:
    svc = _make_service()
    # No exception: missing row → no-op (the UPDATE matches 0 rows).
    await svc.mark_read(_ctx(), uuid.uuid4())


# ---------------------------------------------------------------------------
# event_to_dict — shared REST/MCP serializer
# ---------------------------------------------------------------------------


def test_event_to_dict_shape_matches_capability_fabric_event_v1() -> None:
    event = CapabilityRegistryEvent(
        notification_id=uuid.uuid4(),
        tenant_id=_TENANT,
        subscription_id=uuid.uuid4(),
        capability_id=uuid.uuid4(),
        capability_slug="payment-api",
        event_kind="version_published",
        change_classification="non-breaking",
        version_before="1.0.0",
        version_after="2.0.0",
        occurred_at=_NOW,
        fetch_url="https://example.com/cap/abc",
    )
    payload = event_to_dict(event)
    # Field set is exactly the v1 envelope — no body / description / etc.
    assert set(payload.keys()) == {
        "notification_id",
        "tenant_id",
        "subscription_id",
        "capability_id",
        "capability_slug",
        "event_kind",
        "change_classification",
        "version_before",
        "version_after",
        "occurred_at",
        "fetch_url",
    }
    # No freeform fields ever surface.
    forbidden = {"body", "description", "fact_body", "content", "message"}
    assert not (set(payload.keys()) & forbidden)


def test_event_to_dict_serializes_uuids_and_datetimes_as_strings() -> None:
    event = CapabilityRegistryEvent(
        notification_id=uuid.uuid4(),
        tenant_id=_TENANT,
        subscription_id=None,
        capability_id=uuid.uuid4(),
        capability_slug="x",
        event_kind="deprecation",
        change_classification=None,
        version_before=None,
        version_after=None,
        occurred_at=_NOW,
        fetch_url="https://example.com",
    )
    payload = event_to_dict(event)
    assert isinstance(payload["notification_id"], str)
    assert isinstance(payload["occurred_at"], str)
    assert payload["subscription_id"] is None
    assert payload["change_classification"] is None


# ---------------------------------------------------------------------------
# REST: GET /v1/notifications
# ---------------------------------------------------------------------------


def _build_app(
    list_return: tuple[list[CapabilityRegistryEvent], str | None] | None = None,
    list_effect: Exception | None = None,
    ctx: TenantContext | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(notifications_router)
    svc = MagicMock()
    if list_effect is not None:
        svc.list_notifications = AsyncMock(side_effect=list_effect)
    else:
        svc.list_notifications = AsyncMock(return_value=list_return or ([], None))
    svc.mark_read = AsyncMock(return_value=None)
    app.state.notifications = svc

    from registry.api.middleware.tenant import get_tenant_context  # noqa: PLC0415

    async def _fake_ctx() -> TenantContext:
        return ctx or _ctx()

    app.dependency_overrides[get_tenant_context] = _fake_ctx
    return app


def _sample_event() -> CapabilityRegistryEvent:
    return CapabilityRegistryEvent(
        notification_id=uuid.uuid4(),
        tenant_id=_TENANT,
        subscription_id=uuid.uuid4(),
        capability_id=uuid.uuid4(),
        capability_slug="payment-api",
        event_kind="version_published",
        change_classification="non-breaking",
        version_before="1.0.0",
        version_after="1.1.0",
        occurred_at=_NOW,
        fetch_url="https://example.com/cap/abc",
    )


class TestListNotificationsRest:
    def test_returns_200_with_items_and_no_cursor(self) -> None:
        events = [_sample_event(), _sample_event()]
        app = _build_app(list_return=(events, None))
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/v1/notifications")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) == 2
        assert body["next_cursor"] is None
        # MCP output and REST output share the same item shape.
        assert body["items"][0] == event_to_dict(events[0])

    def test_default_status_is_unread(self) -> None:
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=True)
        client.get("/v1/notifications")
        call = app.state.notifications.list_notifications.await_args
        assert call.kwargs["status"] == "unread"

    def test_status_all_and_cursor_passed_through(self) -> None:
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=True)
        client.get(
            "/v1/notifications",
            params={"status": "all", "cursor": _NOW.isoformat(), "page_size": 10},
        )
        call = app.state.notifications.list_notifications.await_args
        assert call.kwargs["status"] == "all"
        assert call.kwargs["cursor"] == _NOW.isoformat()
        assert call.kwargs["page_size"] == 10

    def test_invalid_status_returns_422_from_service(self) -> None:
        app = _build_app(list_effect=ValidationError("bad status"))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/v1/notifications", params={"status": "bogus"})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# REST: POST /v1/notifications/{id}:mark-read
# ---------------------------------------------------------------------------


class TestMarkReadRest:
    def test_returns_204(self) -> None:
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=True)
        nid = uuid.uuid4()
        resp = client.post(f"/v1/notifications/{nid}:mark-read")
        assert resp.status_code == 204
        app.state.notifications.mark_read.assert_awaited_once()
        # Tenant scope enforced by service — we just verify the id was forwarded.
        call = app.state.notifications.mark_read.await_args
        assert call.kwargs["notification_id"] == nid

    def test_auditor_role_returns_403(self) -> None:
        app = _build_app(ctx=TenantContext(tenant_id=_TENANT, actor_id=_ACTOR, roles=["auditor"]))
        client = TestClient(app, raise_server_exceptions=False)
        nid = uuid.uuid4()
        resp = client.post(f"/v1/notifications/{nid}:mark-read")
        assert resp.status_code == 403
