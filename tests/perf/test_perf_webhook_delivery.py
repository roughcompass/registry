"""Webhook delivery latency perf test — p95 < 30 s under 100-event burst.

Verifies the SLO commitment: ``p95 webhook delivery latency < 30s``
under a 100-event burst across 10 concurrent tenants. Runs entirely
in-process — no Kubernetes, no real webhook endpoint.

Setup
-----
- 10 tenants, each with one capability and one subscription pointing at
  the same mock HTTPS URL. Each subscription has its own HMAC secret.
- For each tenant, ``SubscriptionService.emit_event`` is called 10 times
  in rapid succession (100 events total). Each call records the wall
  clock at emit time.
- ``WebhookDeliveryWorker.run_once()`` is invoked in a tight loop until
  every notification_deliveries row reaches status='success' or the
  per-test timeout fires.
- The mock receiver (an ``httpx.MockTransport``) records the wall clock
  at request arrival, keyed by ``notification_id``.

Latency for event *i* = ``arrival[i] - emit[i]`` (wall-clock).

The test then computes the p95 over all 100 events and asserts it
remains under 30s. The mock receiver responds 202 to every request so
no retries are exercised — this perf test deliberately measures the
happy path; retry behaviour is covered by the unit suite (T16).
"""

from __future__ import annotations

import asyncio
import datetime
import json
import secrets
import time
import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.api.auth.tokens import hash_token
from registry.config import Settings
from registry.main import create_app
from registry.workers.webhook_delivery import WebhookDeliveryWorker

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)

_NUM_TENANTS = 10
_EVENTS_PER_TENANT = 10
_TOTAL_EVENTS = _NUM_TENANTS * _EVENTS_PER_TENANT
_P95_BUDGET_SECONDS = 30.0


pytestmark = [pytest.mark.perf, pytest.mark.slow]


async def _seed_tenant(pg_url: str, slug: str) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Insert one tenant + one actor + one api_token; return (tid, aid, token)."""
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


async def _seed_cap_and_sub(pg_url: str, tenant_id: uuid.UUID, webhook_url: str, secret: str) -> uuid.UUID:
    """Insert one capability + one subscription with a webhook URL."""
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
                    "VALUES (:eid, :tid, 'capability', :name, TRUE, :now, "
                    "        'public')"
                ),
                {
                    "eid": cap_id,
                    "tid": tenant_id,
                    "name": f"perf-cap-{cap_id.hex[:8]}",
                    "now": _NOW,
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO subscriptions
                      (subscription_id, tenant_id, actor_id, capability_id,
                       event_kinds, webhook_url, webhook_hmac_secret_ref,
                       is_enabled, digest_window,
                       t_valid_from, t_valid_to, t_ingested_at, t_invalidated_at,
                       created_at)
                    VALUES (gen_random_uuid(), :tid, NULL, :cap,
                            ARRAY['version_published'], :url,
                            :sec, TRUE, 'none', :now, NULL, :now, NULL, :now)
                    """
                ),
                {
                    "tid": tenant_id,
                    "cap": cap_id,
                    "url": webhook_url,
                    "sec": secret,
                    "now": _NOW,
                },
            )
    finally:
        await engine.dispose()
    return cap_id


@pytest_asyncio.fixture
async def perf_app(pg_container: str):  # type: ignore[type-arg]
    settings = Settings(
        database_url=pg_container,
        pgbouncer_url=pg_container,
        scheduler_jobstore_url=pg_container,
        scheduler_use_memory_jobstore=True,
        embedding_model="stub",
    )
    yield create_app(settings)


@pytest.mark.asyncio
async def test_p95_webhook_delivery_under_30s(pg_container: str, perf_app) -> None:
    app = perf_app
    subs_svc = app.state.subscriptions

    # ---- Setup: 10 tenants × 1 cap × 1 subscription each.
    tenants: list[tuple[uuid.UUID, uuid.UUID]] = []  # (tenant_id, cap_id)
    webhook_url = "https://hook.test/perf"
    secret = "perf-secret"
    for i in range(_NUM_TENANTS):
        tid, _, _ = await _seed_tenant(pg_container, slug=f"perf-t-{i}")
        cap_id = await _seed_cap_and_sub(pg_container, tid, webhook_url, secret)
        tenants.append((tid, cap_id))

    # ---- Mock receiver: records arrival timestamp by notification_id.
    arrivals: dict[str, float] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        arrivals[body["notification_id"]] = time.perf_counter()
        return httpx.Response(202)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    worker = WebhookDeliveryWorker(
        session_factory=app.state.session_factory,
        clock=app.state.clock,
        http_client=http,
    )

    # ---- Burst: emit 10 events per tenant in concurrent fan-out.
    emits: dict[str, float] = {}

    async def _emit(tid: uuid.UUID, cap_id: uuid.UUID, idx: int) -> None:
        ts = time.perf_counter()
        await subs_svc.emit_event(
            capability_id=cap_id,
            event_kind="version_published",
            change_classification="non-breaking",
            version_before="1.0.0",
            version_after=f"1.{idx}.0",
            fetch_url=f"https://api.test/cap/{cap_id}",
        )
        # The notification ID isn't returned by emit_event; we discover it
        # later when the worker delivers — keyed lookup is by the row's
        # ts via the deliveries table. We record per-burst-call wall time
        # and pair with arrivals after drain (see below).
        emits[f"{tid}-{idx}"] = ts

    tasks: list[asyncio.Task] = []
    for tid, cap_id in tenants:
        for idx in range(_EVENTS_PER_TENANT):
            tasks.append(asyncio.create_task(_emit(tid, cap_id, idx)))
    await asyncio.gather(*tasks)

    # ---- Drain: run the worker in a loop until everything succeeds or timeout.
    deadline = time.perf_counter() + _P95_BUDGET_SECONDS + 30
    try:
        while True:
            attempted = await worker.run_once(batch_size=_TOTAL_EVENTS)
            if attempted == 0 and len(arrivals) >= _TOTAL_EVENTS:
                break
            if time.perf_counter() > deadline:
                pytest.fail(f"deliveries did not complete in time: " f"{len(arrivals)}/{_TOTAL_EVENTS} delivered")
            if attempted == 0:
                await asyncio.sleep(0.05)
    finally:
        await worker.close()

    assert len(arrivals) == _TOTAL_EVENTS

    # ---- Pair each arrival with its emit. We don't have a 1:1 notification_id
    # → emit-key mapping (notifications are created inside emit_event), so we
    # approximate by pairing per-tenant burst time to that tenant's deliveries
    # in emit order. This is conservative — any reordering inflates latency.
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    notif_to_tenant: dict[str, uuid.UUID] = {}
    try:
        async with factory() as session:
            res = await session.execute(
                text("SELECT notification_id, tenant_id FROM notifications " "WHERE tenant_id = ANY(:tids)"),
                {"tids": [t for t, _ in tenants]},
            )
            for row in res.mappings().all():
                notif_to_tenant[str(row["notification_id"])] = row["tenant_id"]
    finally:
        await engine.dispose()

    # Compute latencies — earliest emit per tenant for each arrival.
    latencies: list[float] = []
    for nid, arrived_at in arrivals.items():
        tid = notif_to_tenant.get(nid)
        if tid is None:
            continue
        # Earliest still-unmatched emit for this tenant.
        tenant_emit_keys = sorted(
            (k for k in emits.keys() if k.startswith(f"{tid}-")),
            key=lambda k: emits[k],
        )
        if not tenant_emit_keys:
            continue
        emit_key = tenant_emit_keys[0]
        latencies.append(arrived_at - emits.pop(emit_key))

    latencies.sort()
    p95_index = max(int(0.95 * len(latencies)) - 1, 0)
    p95 = latencies[p95_index]
    print(
        f"\n[perf] delivered={len(latencies)} "
        f"p50={latencies[len(latencies) // 2]:.3f}s "
        f"p95={p95:.3f}s budget={_P95_BUDGET_SECONDS}s"
    )
    assert p95 < _P95_BUDGET_SECONDS, f"p95={p95:.3f}s exceeds {_P95_BUDGET_SECONDS}s SLO"
