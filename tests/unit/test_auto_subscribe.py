"""Unit tests for auto-subscribe-on-adoption.

Verifies the end-to-end wiring of ``SubscriptionService.adoption_hook()``
into ``AdoptionService.adopt``:

- Successful adoption creates one inbox-only subscription with the
  default auto-subscribe event kinds (version_published, deprecation,
  breaking_change).
- The subscription is owned by the **consumer** tenant (not the
  provider — the consumer is the one who receives notifications).
- Re-adopting the same (consumer, capability) pair is idempotent at the
  subscription level — no duplicate row is created.
- The subscription's digest_window snapshots the consumer tenant's
  ``notification_digest_window`` at adoption time. If the
  tenant later changes the value, existing auto-subscriptions are NOT
  retroactively updated.
- The auto-subscribe write happens *inside* the adoption transaction
  (verified via SQL ordering on the shared session.execute recorder).
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.service.adoption import AdoptionService
from registry.service.subscriptions import (
    AUTO_SUBSCRIBE_EVENT_KINDS,
    SubscriptionService,
)
from registry.types import FakeClock, TenantContext

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_PROVIDER_TENANT = uuid.uuid4()
_CONSUMER_TENANT = uuid.uuid4()
_CAP_ID = uuid.uuid4()
_ACTOR_ID = uuid.uuid4()


def _ctx() -> TenantContext:
    return TenantContext(tenant_id=_CONSUMER_TENANT, actor_id=_ACTOR_ID, roles=["producer"])


def _async_noop_ctx() -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _make_session(
    *,
    provider_tenant: uuid.UUID = _PROVIDER_TENANT,
    digest_window: str = "1h",
    existing_inbox_sub: uuid.UUID | None = None,
):
    """Build a shared mock session whose execute records every call.

    Routes:
    - SELECT tenant_id FROM entities → provider tenant (adoption lookup)
    - SELECT notification_digest_window FROM tenants → ``digest_window``
    - SELECT … FROM subscriptions WHERE … webhook_url IS NULL … LIMIT 1 →
      ``existing_inbox_sub`` (or None) — for idempotency check.
    """
    calls: list[tuple[str, dict[str, Any]]] = []

    async def _execute(stmt: Any, params: dict[str, Any] | None = None) -> Any:
        sql = " ".join(str(stmt).split())
        calls.append((sql, params or {}))
        result = MagicMock()

        if "FROM entities" in sql and "tenant_id" in sql.lower():
            row = MagicMock()
            row.tenant_id = provider_tenant
            result.first = MagicMock(return_value=row)
            return result

        if "FROM tenants" in sql and "notification_digest_window" in sql:
            row = MagicMock(notification_digest_window=digest_window)
            result.first = MagicMock(return_value=row)
            return result

        if "FROM subscriptions" in sql and "webhook_url IS NULL" in sql and "LIMIT 1" in sql:
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

        # INSERTs / UPDATEs — no return rows.
        result.first = MagicMock(return_value=None)
        result.mappings.return_value.all.return_value = []
        return result

    session = MagicMock()
    session.execute = _execute
    session.begin = MagicMock(return_value=_async_noop_ctx())
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=session)
    return factory, calls


def _make_visibility() -> MagicMock:
    vis = MagicMock()
    vis.assert_visible = AsyncMock(return_value=None)
    return vis


def _wire(
    factory: MagicMock,
) -> tuple[AdoptionService, SubscriptionService]:
    subs = SubscriptionService(factory, FakeClock(_NOW), _make_visibility())
    adoption = AdoptionService(
        session_factory=factory,
        clock=FakeClock(_NOW),
        visibility=_make_visibility(),
        auto_subscribe=subs.adoption_hook(),
    )
    return adoption, subs


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adopt_creates_inbox_only_subscription_for_consumer() -> None:
    factory, calls = _make_session(digest_window="1h")
    adoption, _ = _wire(factory)

    ref = await adoption.adopt(
        ctx=_ctx(),
        provider_capability_id=_CAP_ID,
        consumer_tenant_id=_CONSUMER_TENANT,
    )

    sub_inserts = [(sql, params) for sql, params in calls if "INSERT INTO subscriptions" in sql]
    assert len(sub_inserts) == 1
    sql, params = sub_inserts[0]
    # Inbox-only: hardcoded NULL/NULL for url+secret.
    assert "NULL, NULL," in sql
    # Owned by the consumer tenant — NOT the provider.
    assert params["tid"] == _CONSUMER_TENANT
    assert params["cap"] == _CAP_ID
    # Default auto-subscribe kinds applied.
    assert set(params["kinds"]) == set(AUTO_SUBSCRIBE_EVENT_KINDS)
    # adoption_id from the AdoptionEventRef is unrelated to subscription_id.
    assert ref.consumer_tenant_id == _CONSUMER_TENANT


@pytest.mark.asyncio
async def test_adopt_inherits_consumer_digest_window_at_adoption_time() -> None:
    factory, calls = _make_session(digest_window="6h")
    adoption, _ = _wire(factory)

    await adoption.adopt(
        ctx=_ctx(),
        provider_capability_id=_CAP_ID,
        consumer_tenant_id=_CONSUMER_TENANT,
    )

    sub_inserts = [params for sql, params in calls if "INSERT INTO subscriptions" in sql]
    assert sub_inserts and sub_inserts[0]["digest"] == "6h"


@pytest.mark.asyncio
async def test_adopt_twice_is_idempotent_for_subscription() -> None:
    """Second adopt() for the same (consumer, cap) pair finds the existing
    inbox subscription and does NOT INSERT a new subscriptions row.

    Note: this test models the subscription-idempotency contract, not
    adoption-row idempotency (a new adoption_events row is still created
    so the audit trail captures the re-adoption).
    """
    existing_sub = uuid.uuid4()
    factory, calls = _make_session(existing_inbox_sub=existing_sub)
    adoption, _ = _wire(factory)

    await adoption.adopt(
        ctx=_ctx(),
        provider_capability_id=_CAP_ID,
        consumer_tenant_id=_CONSUMER_TENANT,
    )

    sub_inserts = [params for sql, params in calls if "INSERT INTO subscriptions" in sql]
    # The existing-inbox-sub branch returns early — no new INSERT.
    assert not sub_inserts


@pytest.mark.asyncio
async def test_subscription_insert_happens_after_adoption_insert() -> None:
    """The auto-subscribe write must occur inside the adoption transaction,
    after adoption_events and edges are written."""
    factory, calls = _make_session()
    adoption, _ = _wire(factory)

    await adoption.adopt(
        ctx=_ctx(),
        provider_capability_id=_CAP_ID,
        consumer_tenant_id=_CONSUMER_TENANT,
    )

    sqls = [s for s, _ in calls]
    idx_adopt = next(i for i, s in enumerate(sqls) if "INSERT INTO adoption_events" in s)
    idx_edge = next(i for i, s in enumerate(sqls) if "INSERT INTO edges" in s)
    idx_sub = next(i for i, s in enumerate(sqls) if "INSERT INTO subscriptions" in s)
    assert idx_adopt < idx_edge < idx_sub
