"""End-to-end adoption + subscription integration tests.

Two headline scenarios:

1. Adoption flow:
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
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

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
from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    bearer_headers,
    patch_validator_for_actor,
)

type _AppClient = tuple[AsyncClient, EntitlementAuthHarness]

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)
_WEBHOOK_SECRET = "test-secret-xyz"


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


async def _materialise_persona(harness: EntitlementAuthHarness, slug: str, roles: list[str]) -> uuid.UUID:
    """JIT-materialise a persona and return its tenant_id."""

    persona = harness.add_persona(slug, roles=roles)
    harness.configure_fetcher_for(persona)
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch_validator_for_actor(persona):
            resp = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))
            assert resp.status_code == 200, resp.text
    return uuid.UUID(resp.json()["tenant_id"])


@pytest_asyncio.fixture
async def app_client(pg_container: str) -> AsyncIterator[_AppClient]:
    async with EntitlementAuthHarness(pg_container) as harness:
        transport = ASGITransport(app=harness.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, harness


# ---------------------------------------------------------------------------
# Scenario 1 — adoption flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adoption_flow_with_acl(pg_container: str, app_client: _AppClient) -> None:
    client, harness = app_client
    a_tid = await _materialise_persona(harness, f"adopt-flow-a-{uuid.uuid4().hex[:6]}", ["producer", "consumer"])
    persona_b = harness.get(list(harness._personas)[-2]) if len(harness._personas) > 1 else None

    # Re-materialise deterministically.
    slug_a = f"adopt-flow-a-{uuid.uuid4().hex[:6]}"
    slug_b = f"adopt-flow-b-{uuid.uuid4().hex[:6]}"
    slug_c = f"adopt-flow-c-{uuid.uuid4().hex[:6]}"

    persona_a = harness.add_persona(slug_a, roles=["producer", "consumer", "admin"])
    persona_b = harness.add_persona(slug_b, roles=["producer", "consumer", "admin"])
    persona_c = harness.add_persona(slug_c, roles=["producer", "consumer", "admin"])

    harness.configure_fetcher_for(persona_a)
    with patch_validator_for_actor(persona_a):
        r = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug_a))
    a_tid = uuid.UUID(r.json()["tenant_id"])

    harness.configure_fetcher_for(persona_b)
    with patch_validator_for_actor(persona_b):
        r = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug_b))
    b_tid = uuid.UUID(r.json()["tenant_id"])

    harness.configure_fetcher_for(persona_c)
    with patch_validator_for_actor(persona_c):
        await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug_c))

    cap_id = await _seed_payment_api(pg_container, tenant_id=a_tid, shared_with=[b_tid])

    # Tenant B adopts with version_pin ^2.0
    harness.configure_fetcher_for(persona_b)
    with patch_validator_for_actor(persona_b):
        resp_b = await client.post(
            f"/v1/capabilities/{cap_id}/adoptions",
            headers=bearer_headers(tenant_slug=slug_b),
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
    harness.configure_fetcher_for(persona_c)
    with patch_validator_for_actor(persona_c):
        resp_c = await client.post(
            f"/v1/capabilities/{cap_id}/adoptions",
            headers=bearer_headers(tenant_slug=slug_c),
            json={},
        )
    assert resp_c.status_code in (403, 404), resp_c.text


# ---------------------------------------------------------------------------
# Scenario 2 — webhook delivery with HMAC signing + payload-minimality
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_delivery_signs_and_delivers_minimal_payload(pg_container: str, app_client: _AppClient) -> None:
    _client, harness = app_client

    slug_a = f"webhook-a-{uuid.uuid4().hex[:6]}"
    slug_b = f"webhook-b-{uuid.uuid4().hex[:6]}"

    persona_a = harness.add_persona(slug_a, roles=["producer", "consumer"])
    persona_b = harness.add_persona(slug_b, roles=["producer", "consumer"])

    harness.configure_fetcher_for(persona_a)
    with patch_validator_for_actor(persona_a):
        r = await _client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug_a))
    a_tid = uuid.UUID(r.json()["tenant_id"])

    harness.configure_fetcher_for(persona_b)
    with patch_validator_for_actor(persona_b):
        r = await _client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug_b))
    b_tid = uuid.UUID(r.json()["tenant_id"])

    cap_id = await _seed_payment_api(pg_container, tenant_id=a_tid, shared_with=[b_tid])

    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
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

    subs_svc = harness.app.state.subscriptions
    count = await subs_svc.emit_event(
        capability_id=cap_id,
        event_kind="version_published",
        change_classification="non-breaking",
        version_before="2.3.5",
        version_after="2.4.0",
        fetch_url=f"https://api.test/v1/capabilities/{cap_id}",
    )
    assert count == 1

    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = req.content
        captured["sig"] = req.headers.get(SIGNATURE_HEADER)
        captured["url"] = str(req.url)
        return httpx.Response(202)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    worker = WebhookDeliveryWorker(
        session_factory=harness.app.state.session_factory,
        clock=harness.app.state.clock,
        http_client=http,
    )
    try:
        attempted = await worker.run_once()
    finally:
        await worker.close()
    assert attempted == 1, "the worker did not claim the pending delivery row"

    body = json.loads(captured["body"])
    assert body["version_after"] == "2.4.0"
    assert body["version_before"] == "2.3.5"
    assert body["event_kind"] == "version_published"
    forbidden = {"body", "description", "fact_body", "content", "message"}
    assert not (set(body.keys()) & forbidden)

    assert verify_signature(captured["body"], _WEBHOOK_SECRET, captured["sig"])

    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            res = await session.execute(
                text("SELECT status, http_status FROM notification_deliveries WHERE tenant_id = :tid"),
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
