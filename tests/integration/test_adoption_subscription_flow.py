"""End-to-end adoption + subscription integration tests.

Two headline scenarios:

1. Adoption flow (the headline scenario from tasks.md §T22):
   - Tenant A publishes PaymentAPI (tenant-shared, ACL=[B]).
   - Tenant B adopts with ``version_pin=^2.0``; the provides_to edge is
     created; an auto-subscription appears in B's subscriptions table.
   - Tenant C's adoption attempt → 403.

2. Subscription webhook delivery:
   - Tenant B's auto-subscription is upgraded to point at a mock webhook URL
     and given an HMAC secret.
   - PaymentAPI v2.4.0 is "published" via SubscriptionService.emit_event.
   - WebhookDeliveryWorker.run_once dispatches the row; the mock
     receiver records the payload + signature.
   - Assert: ``version_after='2.4.0'`` is present, no body/description
     freeform text is included, HMAC verifies, and the
     notification_deliveries row ends up with status='success'.

3. Digest window: 3 events from one tenant with digest_window='15m' →
   ``make_digest_envelope`` returns a single envelope with item_count=3.
"""

from __future__ import annotations

import datetime
import json
import secrets
import uuid
from typing import Any

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.api.auth.tokens import hash_token
from registry.config import Settings
from registry.main import create_app
from registry.service.visibility import (
    VISIBILITY_PUBLIC,
    VISIBILITY_TENANT_SHARED,
)
from registry.types import CapabilityRegistryEvent
from registry.workers.webhook_delivery import (
    SIGNATURE_HEADER,
    WebhookDeliveryWorker,
    make_digest_envelope,
    verify_signature,
)

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)
_WEBHOOK_SECRET = "test-secret-xyz"


async def _seed_tenant_with_token(pg_url: str, *, slug: str) -> tuple[uuid.UUID, uuid.UUID, str]:
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    raw_token = secrets.token_urlsafe(24)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO tenants (tenant_id, slug, display_name, "
                    "created_at, is_active) VALUES "
                    "(:tid, :slug, :slug, :now, TRUE)"
                ),
                {"tid": tenant_id, "slug": slug, "now": _NOW},
            )
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, display_name, "
                    "created_at) VALUES (:aid, :tid, :dn, :now)"
                ),
                {"aid": actor_id, "tid": tenant_id, "dn": f"actor-{slug}", "now": _NOW},
            )
            await session.execute(
                text(
                    "INSERT INTO api_tokens "
                    "(token_id, tenant_id, actor_id, token_hash, roles, created_at) "
                    "VALUES (gen_random_uuid(), :tid, :aid, :th, :roles, :now)"
                ),
                {
                    "tid": tenant_id,
                    "aid": actor_id,
                    "th": hash_token(raw_token),
                    "roles": ["producer", "consumer", "admin"],
                    "now": _NOW,
                },
            )
    finally:
        await engine.dispose()
    return tenant_id, actor_id, raw_token


async def _seed_payment_api(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    shared_with: list[uuid.UUID] | None,
) -> uuid.UUID:
    cap_id = uuid.uuid4()
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO entities "
                    "(entity_id, tenant_id, entity_type, name, is_active, "
                    " created_at, visibility) "
                    "VALUES (:eid, :tid, 'capability', 'PaymentAPI', TRUE, :now, :vis)"
                ),
                {
                    "eid": cap_id,
                    "tid": tenant_id,
                    "now": _NOW,
                    "vis": (VISIBILITY_TENANT_SHARED if shared_with else VISIBILITY_PUBLIC),
                },
            )
            if shared_with:
                await session.execute(
                    text(
                        "INSERT INTO attributes "
                        "(attr_id, tenant_id, entity_id, key, value, "
                        " t_valid_from, t_valid_to, t_ingested_at, "
                        " t_invalidated_at) "
                        "VALUES (gen_random_uuid(), :tid, :eid, "
                        "        'shared_with_tenants', CAST(:val AS jsonb), "
                        "        :now, NULL, :now, NULL)"
                    ),
                    {
                        "tid": tenant_id,
                        "eid": cap_id,
                        "val": json.dumps([str(t) for t in shared_with]),
                        "now": _NOW,
                    },
                )
    finally:
        await engine.dispose()
    return cap_id


@pytest_asyncio.fixture
async def app_client(pg_container: str):  # type: ignore[type-arg]
    settings = Settings(
        database_url=pg_container,
        pgbouncer_url=pg_container,
        scheduler_jobstore_url=pg_container,
        scheduler_use_memory_jobstore=True,
        embedding_model="stub",
    )
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, app


# ---------------------------------------------------------------------------
# Scenario 1 — adoption flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adoption_flow_with_acl(pg_container: str, app_client) -> None:
    client, app = app_client
    a_tid, _, _ = await _seed_tenant_with_token(pg_container, slug="adopt-flow-a")
    b_tid, _, b_token = await _seed_tenant_with_token(pg_container, slug="adopt-flow-b")
    _, _, c_token = await _seed_tenant_with_token(pg_container, slug="adopt-flow-c")

    cap_id = await _seed_payment_api(pg_container, tenant_id=a_tid, shared_with=[b_tid])

    # Tenant B adopts with version_pin ^2.0
    resp_b = await client.post(
        f"/v1/capabilities/{cap_id}/adoptions",
        headers={"Authorization": f"Bearer {b_token}"},
        json={"version_pin": "^2.0"},
    )
    assert resp_b.status_code == 201, resp_b.text

    # provides_to edge exists.
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            row = await session.execute(
                text(
                    "SELECT COUNT(*) FROM edges "
                    "WHERE rel = 'provides_to' AND src_entity_id = :cap "
                    "  AND t_invalidated_at IS NULL"
                ),
                {"cap": cap_id},
            )
            assert (row.scalar() or 0) >= 1
            # Auto-subscription created for the consumer tenant.
            row = await session.execute(
                text(
                    "SELECT COUNT(*) FROM subscriptions "
                    "WHERE tenant_id = :tid AND capability_id = :cap "
                    "  AND t_invalidated_at IS NULL"
                ),
                {"tid": b_tid, "cap": cap_id},
            )
            assert (row.scalar() or 0) == 1
    finally:
        await engine.dispose()

    # Tenant C is outside the ACL → 403/404.
    resp_c = await client.post(
        f"/v1/capabilities/{cap_id}/adoptions",
        headers={"Authorization": f"Bearer {c_token}"},
        json={},
    )
    assert resp_c.status_code in (403, 404), resp_c.text


# ---------------------------------------------------------------------------
# Scenario 2 — webhook delivery with HMAC signing + payload-minimality
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_delivery_signs_and_delivers_minimal_payload(pg_container: str, app_client) -> None:
    _client, app = app_client
    a_tid, _, _ = await _seed_tenant_with_token(pg_container, slug="webhook-a")
    b_tid, _, _ = await _seed_tenant_with_token(pg_container, slug="webhook-b")
    cap_id = await _seed_payment_api(pg_container, tenant_id=a_tid, shared_with=[b_tid])

    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        # Insert a subscription with a webhook URL + secret.
        sub_id = uuid.uuid4()
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    """
                    INSERT INTO subscriptions
                      (subscription_id, tenant_id, actor_id, capability_id,
                       event_kinds, webhook_url, webhook_hmac_secret_ref,
                       is_enabled, digest_window,
                       t_valid_from, t_valid_to, t_ingested_at, t_invalidated_at,
                       created_at)
                    VALUES (:sid, :tid, NULL, :cap,
                            ARRAY['version_published'], 'https://hook.test/x',
                            :sec, TRUE, 'none', :now, NULL, :now, NULL, :now)
                    """
                ),
                {
                    "sid": sub_id,
                    "tid": b_tid,
                    "cap": cap_id,
                    "sec": _WEBHOOK_SECRET,
                    "now": _NOW,
                },
            )
    finally:
        await engine.dispose()

    # Fire emit_event via the app's SubscriptionService → inserts notifications
    # + notification_deliveries rows.
    subs_svc = app.state.subscriptions
    count = await subs_svc.emit_event(
        capability_id=cap_id,
        event_kind="version_published",
        change_classification="non-breaking",
        version_before="2.3.5",
        version_after="2.4.0",
        fetch_url=f"https://api.test/v1/capabilities/{cap_id}",
    )
    assert count == 1

    # Run the worker once against a MockTransport that captures the request.
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = req.content
        captured["sig"] = req.headers.get(SIGNATURE_HEADER)
        captured["url"] = str(req.url)
        return httpx.Response(202)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    # Use the live app clock — emit_event() wrote next_retry_at at the same
    # wall-clock, so the worker must compare with the matching clock.
    worker = WebhookDeliveryWorker(
        session_factory=app.state.session_factory,
        clock=app.state.clock,
        http_client=http,
    )
    try:
        attempted = await worker.run_once()
    finally:
        await worker.close()
    assert attempted == 1, "the worker did not claim the pending delivery row"

    # Payload contract: version_after=2.4.0 + no freeform description fields.
    body = json.loads(captured["body"])
    assert body["version_after"] == "2.4.0"
    assert body["version_before"] == "2.3.5"
    assert body["event_kind"] == "version_published"
    forbidden = {"body", "description", "fact_body", "content", "message"}
    assert not (set(body.keys()) & forbidden)

    # HMAC signs the body with the subscription's secret.
    assert verify_signature(captured["body"], _WEBHOOK_SECRET, captured["sig"])

    # notification_deliveries row updated → success.
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            res = await session.execute(
                text("SELECT status, http_status FROM notification_deliveries " "WHERE tenant_id = :tid"),
                {"tid": b_tid},
            )
            row = res.first()
            assert row is not None
            assert row.status == "success"
            assert row.http_status == 202
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Scenario 3 — digest window envelope
# ---------------------------------------------------------------------------


def test_digest_envelope_combines_three_events_into_one_envelope() -> None:
    """3 events from one tenant with digest_window='15m' → single envelope."""
    tenant = uuid.uuid4()
    events = [
        CapabilityRegistryEvent(
            notification_id=uuid.uuid4(),
            tenant_id=tenant,
            subscription_id=uuid.uuid4(),
            capability_id=uuid.uuid4(),
            capability_slug="payment-api",
            event_kind="version_published",
            change_classification="non-breaking",
            version_before="1.0.0",
            version_after=f"1.{i + 1}.0",
            occurred_at=_NOW + datetime.timedelta(minutes=i),
            fetch_url="https://api.test/v1/capabilities/abc",
        )
        for i in range(3)
    ]
    env = make_digest_envelope(tenant, events, window="15m", now=_NOW)
    assert env["envelope_type"] == "CapabilityRegistry.Digest"
    assert env["version"] == "v1"
    assert env["item_count"] == 3
    assert len(env["items"]) == 3
    # Same payload-minimality rule applies to each item in the envelope.
    forbidden = {"body", "description", "fact_body"}
    for item in env["items"]:
        assert not (set(item.keys()) & forbidden)
