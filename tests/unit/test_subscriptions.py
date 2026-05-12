"""Unit tests for SubscriptionService.

All SQL is mocked at ``session.execute`` via an SQL-string-keyed router
so each test returns canned rows per query shape.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.exceptions import NotFoundError, ValidationError
from registry.service.subscriptions import (
    AUTO_SUBSCRIBE_EVENT_KINDS,
    VALID_EVENT_KINDS,
    SubscriptionService,
)
from registry.types import FakeClock, TenantContext

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_TENANT = uuid.uuid4()
_ACTOR = uuid.uuid4()


def _ctx(tenant: uuid.UUID | None = None) -> TenantContext:
    return TenantContext(tenant_id=tenant or _TENANT, actor_id=_ACTOR, roles=["consumer"])


def _make_session(
    *,
    digest_window: str = "none",
    capability_slug: str | None = "payment-api",
    existing_inbox_sub: uuid.UUID | None = None,
    subs_for_event: list[dict] | None = None,
    sub_for_delete: dict | None = None,
):
    """Build an AsyncMock session whose ``execute`` routes by SQL keywords."""
    executed: list[tuple[str, dict | None]] = []

    async def _execute(stmt: Any, params: dict | None = None):
        sql = " ".join(str(stmt).split())
        executed.append((sql, params))
        result = MagicMock()

        if "FROM tenants" in sql and "notification_digest_window" in sql:
            row = MagicMock(notification_digest_window=digest_window)
            result.first = MagicMock(return_value=row)
            return result

        if "FROM entities" in sql and "entity_id = :eid" in sql:
            if capability_slug is None:
                result.first = MagicMock(return_value=None)
            else:
                row = MagicMock(name=capability_slug)
                # Avoid Mock auto-attribute weirdness on the .name attribute.
                row.configure_mock(**{"name": capability_slug})
                result.first = MagicMock(return_value=row)
            return result

        if "FROM subscriptions" in sql and "webhook_url IS NULL" in sql and "LIMIT 1" in sql:
            # auto_subscribe duplicate check
            if existing_inbox_sub is None:
                result.first = MagicMock(return_value=None)
            else:
                result.first = MagicMock(
                    return_value=MagicMock(
                        subscription_id=existing_inbox_sub,
                        event_kinds=list(AUTO_SUBSCRIBE_EVENT_KINDS),
                    )
                )
            return result

        if "FROM subscriptions" in sql and "= ANY(event_kinds)" in sql:
            # emit_event subscriber lookup
            rows = []
            for s in subs_for_event or []:
                rows.append(
                    MagicMock(
                        subscription_id=s["subscription_id"],
                        tenant_id=s["tenant_id"],
                        webhook_url=s.get("webhook_url"),
                    )
                )
            result.all = MagicMock(return_value=rows)
            return result

        if "FROM subscriptions" in sql and "subscription_id = :sid" in sql and "t_invalidated_at FROM" in sql:
            # delete_subscription lookup
            if sub_for_delete is None:
                result.first = MagicMock(return_value=None)
            else:
                result.first = MagicMock(
                    return_value=MagicMock(
                        subscription_id=sub_for_delete["subscription_id"],
                        t_invalidated_at=sub_for_delete.get("t_invalidated_at"),
                    )
                )
            return result

        if "FROM subscriptions" in sql and "ORDER BY t_valid_from DESC" in sql:
            # list_subscriptions
            result.mappings.return_value.all.return_value = []
            return result

        # INSERT / UPDATE — no return rows
        result.first = MagicMock(return_value=None)
        result.all = MagicMock(return_value=[])
        result.mappings.return_value.all.return_value = []
        return result

    session = MagicMock()
    session.execute = _execute
    session.executed = executed  # type: ignore[attr-defined]
    session.begin = MagicMock(return_value=_async_ctx())
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=session)
    return factory, session


def _async_ctx() -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _make_visibility(visible: bool = True) -> MagicMock:
    vis = MagicMock()
    if visible:
        vis.assert_visible = AsyncMock(return_value=None)
    else:
        vis.assert_visible = AsyncMock(side_effect=PermissionError("not visible"))
    return vis


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_valid_event_kinds_is_closed_vocabulary() -> None:
    assert VALID_EVENT_KINDS == {
        "version_published",
        "deprecation",
        "breaking_change",
        "conflict_added",
        "integration_added",
    }


def test_auto_subscribe_default_kinds_subset_of_valid() -> None:
    assert set(AUTO_SUBSCRIBE_EVENT_KINDS).issubset(VALID_EVENT_KINDS)


@pytest.mark.asyncio
async def test_create_subscription_rejects_empty_event_kinds() -> None:
    factory, _ = _make_session()
    svc = SubscriptionService(factory, FakeClock(_NOW), _make_visibility())
    with pytest.raises(ValidationError):
        await svc.create_subscription(
            ctx=_ctx(),
            capability_id=uuid.uuid4(),
            event_kinds=[],
        )


@pytest.mark.asyncio
async def test_create_subscription_rejects_unknown_event_kind() -> None:
    factory, _ = _make_session()
    svc = SubscriptionService(factory, FakeClock(_NOW), _make_visibility())
    with pytest.raises(ValidationError):
        await svc.create_subscription(
            ctx=_ctx(),
            capability_id=uuid.uuid4(),
            event_kinds=["not_a_real_kind"],
        )


@pytest.mark.asyncio
async def test_create_subscription_rejects_invisible_capability() -> None:
    factory, _ = _make_session()
    vis = _make_visibility(visible=False)
    svc = SubscriptionService(factory, FakeClock(_NOW), vis)
    with pytest.raises(PermissionError):
        await svc.create_subscription(
            ctx=_ctx(),
            capability_id=uuid.uuid4(),
            event_kinds=["version_published"],
        )


# ---------------------------------------------------------------------------
# Create / digest window inheritance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_subscription_inherits_tenant_digest_window() -> None:
    factory, session = _make_session(digest_window="15m")
    svc = SubscriptionService(factory, FakeClock(_NOW), _make_visibility())
    sid = await svc.create_subscription(
        ctx=_ctx(),
        capability_id=uuid.uuid4(),
        event_kinds=["version_published"],
        webhook_url="https://example.com/webhook",
        webhook_hmac_secret_ref="vault:abc",
    )
    assert isinstance(sid, uuid.UUID)
    # Find the INSERT and verify digest was set to '15m'.
    inserts = [params for sql, params in session.executed if "INSERT INTO subscriptions" in sql]
    assert inserts and inserts[0]["digest"] == "15m"
    assert inserts[0]["url"] == "https://example.com/webhook"
    assert inserts[0]["secret"] == "vault:abc"


@pytest.mark.asyncio
async def test_create_subscription_falls_back_to_none_for_unknown_digest() -> None:
    factory, session = _make_session(digest_window="bogus")
    svc = SubscriptionService(factory, FakeClock(_NOW), _make_visibility())
    await svc.create_subscription(
        ctx=_ctx(),
        capability_id=uuid.uuid4(),
        event_kinds=["version_published"],
    )
    inserts = [params for sql, params in session.executed if "INSERT INTO subscriptions" in sql]
    assert inserts[0]["digest"] == "none"


# ---------------------------------------------------------------------------
# auto_subscribe — idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_subscribe_returns_existing_when_duplicate() -> None:
    existing = uuid.uuid4()
    factory, session = _make_session(existing_inbox_sub=existing)
    svc = SubscriptionService(factory, FakeClock(_NOW), _make_visibility())
    # Use the session directly, mimicking what AdoptionService does inside its tx.
    sid = await svc.auto_subscribe(
        session=session,
        tenant_id=_TENANT,
        actor_id=_ACTOR,
        capability_id=uuid.uuid4(),
    )
    assert sid == existing
    inserts = [params for sql, params in session.executed if "INSERT INTO subscriptions" in sql]
    assert not inserts, "duplicate auto_subscribe must not insert"


@pytest.mark.asyncio
async def test_auto_subscribe_creates_inbox_only_subscription() -> None:
    factory, session = _make_session(digest_window="1h")
    svc = SubscriptionService(factory, FakeClock(_NOW), _make_visibility())
    cap = uuid.uuid4()
    sid = await svc.auto_subscribe(
        session=session,
        tenant_id=_TENANT,
        actor_id=_ACTOR,
        capability_id=cap,
    )
    assert isinstance(sid, uuid.UUID)
    inserts = [(sql, params) for sql, params in session.executed if "INSERT INTO subscriptions" in sql]
    assert len(inserts) == 1
    sql, params = inserts[0]
    # auto_subscribe hardcodes NULL for webhook fields — verify via SQL text.
    assert "NULL, NULL," in sql
    assert params["cap"] == cap
    assert params["digest"] == "1h"
    assert set(params["kinds"]) == set(AUTO_SUBSCRIBE_EVENT_KINDS)


# ---------------------------------------------------------------------------
# emit_event — fan-out
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_event_rejects_unknown_kind() -> None:
    factory, _ = _make_session()
    svc = SubscriptionService(factory, FakeClock(_NOW), _make_visibility())
    with pytest.raises(ValidationError):
        await svc.emit_event(
            capability_id=uuid.uuid4(),
            event_kind="nope",
            change_classification=None,
            version_before=None,
            version_after="2.0.0",
            fetch_url="https://example.com/cap/abc",
        )


@pytest.mark.asyncio
async def test_emit_event_raises_when_capability_missing() -> None:
    factory, _ = _make_session(capability_slug=None)
    svc = SubscriptionService(factory, FakeClock(_NOW), _make_visibility())
    with pytest.raises(NotFoundError):
        await svc.emit_event(
            capability_id=uuid.uuid4(),
            event_kind="version_published",
            change_classification=None,
            version_before=None,
            version_after="2.0.0",
            fetch_url="https://example.com/cap/abc",
        )


@pytest.mark.asyncio
async def test_emit_event_inserts_notification_per_subscription() -> None:
    cap = uuid.uuid4()
    s1 = {"subscription_id": uuid.uuid4(), "tenant_id": uuid.uuid4(), "webhook_url": None}
    s2 = {
        "subscription_id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "webhook_url": "https://hook.example.com",
    }
    factory, session = _make_session(subs_for_event=[s1, s2])
    svc = SubscriptionService(factory, FakeClock(_NOW), _make_visibility())

    count = await svc.emit_event(
        capability_id=cap,
        event_kind="version_published",
        change_classification="non-breaking",
        version_before="1.0.0",
        version_after="1.1.0",
        fetch_url="https://example.com/cap/abc",
    )
    assert count == 2

    notif_inserts = [params for sql, params in session.executed if "INSERT INTO notifications" in sql]
    delivery_inserts = [params for sql, params in session.executed if "INSERT INTO notification_deliveries" in sql]
    assert len(notif_inserts) == 2
    # Only the webhook-equipped subscription gets a deliveries row.
    assert len(delivery_inserts) == 1
    assert delivery_inserts[0]["url"] == "https://hook.example.com"

    # Payload-minimal: no body, description, or freeform field in the INSERT.
    forbidden = {"body", "description", "fact_body", "content"}
    for params in notif_inserts:
        assert not (set(params.keys()) & forbidden)


@pytest.mark.asyncio
async def test_emit_event_no_subscribers_returns_zero() -> None:
    factory, session = _make_session(subs_for_event=[])
    svc = SubscriptionService(factory, FakeClock(_NOW), _make_visibility())
    count = await svc.emit_event(
        capability_id=uuid.uuid4(),
        event_kind="deprecation",
        change_classification=None,
        version_before="1.0.0",
        version_after="1.0.0",
        fetch_url="https://example.com/cap/abc",
    )
    assert count == 0


# ---------------------------------------------------------------------------
# delete_subscription
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_subscription_not_found_raises() -> None:
    factory, _ = _make_session(sub_for_delete=None)
    svc = SubscriptionService(factory, FakeClock(_NOW), _make_visibility())
    with pytest.raises(NotFoundError):
        await svc.delete_subscription(_ctx(), uuid.uuid4())


@pytest.mark.asyncio
async def test_delete_subscription_soft_deletes_and_is_idempotent() -> None:
    sid = uuid.uuid4()
    factory, session = _make_session(sub_for_delete={"subscription_id": sid, "t_invalidated_at": None})
    svc = SubscriptionService(factory, FakeClock(_NOW), _make_visibility())
    await svc.delete_subscription(_ctx(), sid)
    updates = [
        params for sql, params in session.executed if "UPDATE subscriptions" in sql and "t_invalidated_at = :now" in sql
    ]
    assert len(updates) == 1
    assert updates[0]["sid"] == sid

    # Second call — already invalidated → no-op
    factory2, session2 = _make_session(sub_for_delete={"subscription_id": sid, "t_invalidated_at": _NOW})
    svc2 = SubscriptionService(factory2, FakeClock(_NOW), _make_visibility())
    await svc2.delete_subscription(_ctx(), sid)
    updates2 = [
        params
        for sql, params in session2.executed
        if "UPDATE subscriptions" in sql and "t_invalidated_at = :now" in sql
    ]
    assert not updates2


# ---------------------------------------------------------------------------
# adoption_hook adapter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adoption_hook_calls_auto_subscribe_with_consumer_tenant() -> None:
    factory, session = _make_session()
    svc = SubscriptionService(factory, FakeClock(_NOW), _make_visibility())
    consumer = uuid.uuid4()
    provider = uuid.uuid4()
    cap = uuid.uuid4()
    from registry.types import AdoptionEventRef

    adoption = AdoptionEventRef(
        adoption_id=uuid.uuid4(),
        tenant_id=provider,
        provider_capability_id=cap,
        consumer_tenant_id=consumer,
        actor_id=_ACTOR,
        intent=None,
        version_pin=None,
        t_valid_from=_NOW,
        t_valid_to=None,
        t_ingested_at=_NOW,
        t_invalidated_at=None,
    )
    hook = svc.adoption_hook()
    await hook(session=session, ctx=_ctx(consumer), adoption=adoption)

    inserts = [params for sql, params in session.executed if "INSERT INTO subscriptions" in sql]
    assert len(inserts) == 1
    assert inserts[0]["tid"] == consumer  # consumer tenant_id, not provider
    assert inserts[0]["cap"] == cap
