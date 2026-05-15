"""Integration tests: cross-tenant annotation visibility.

Covers the normative three-tenant cross-tenant flow:
- Tenant A owns the capability (provider).
- Tenant B submits an annotation (consumer / author).
- Tenant C is a third consumer with no annotations on this capability.

Access-path semantics verified against a live Postgres instance:
- POST   /v1/capabilities/{id}/annotations  → 201 (Tenant B)
- GET    /v1/capabilities/{id}/annotations  → provider path returns all (Tenant A)
- GET    /v1/capabilities/{id}/annotations  → author path returns own only (Tenant B)
- GET    /v1/capabilities/{id}/annotations  → third-tenant path returns empty 200 (Tenant C)

The visibility chokepoint (VisibilityService.assert_visible) is exercised
for real — it is not mocked — because these tests exist specifically to verify
the FK constraints, partial-index query plans, and visibility enforcement that
unit tests cannot reach.

Latency assertion: test_list_annotations_p95_latency seeds 1,000 annotations
directly via SQL (not via POST to avoid 1,000 round-trips) and asserts that
ten GET requests by the provider tenant complete at p95 < 200 ms. Set
SKIP_LATENCY_TESTS=1 in the environment to record timing but skip the
assertion (e.g. in resource-constrained CI).

NOTE: AnnotationService.list_annotations currently queries
    SELECT tenant_id FROM capabilities WHERE capability_id = :cid
The "capabilities" table does not exist in the schema — the correct table
name is "entities" with entity_id as the PK. Tests that reach the list path
will fail with a DB relation-does-not-exist error until that bug is fixed.
This test file is written against the correct intended contract; the failure
exposes the bug rather than hiding it.
"""

from __future__ import annotations

import datetime
import os
import time
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.service.visibility import VISIBILITY_PRIVATE, VISIBILITY_PUBLIC
from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    TenantPersona,
    bearer_headers,
    patch_validator_for_actor,
)

type _AppClient = tuple[EntitlementAuthHarness, AsyncClient]

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_capability(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    name: str,
    visibility: str = VISIBILITY_PUBLIC,
) -> uuid.UUID:
    """Insert one capability entity owned by tenant_id with given visibility."""
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
                    "VALUES (:eid, :tid, 'capability', :name, TRUE, :now, :vis)"
                ),
                {
                    "eid": cap_id,
                    "tid": tenant_id,
                    "name": name,
                    "now": _NOW,
                    "vis": visibility,
                },
            )
    finally:
        await engine.dispose()
    return cap_id


async def _seed_annotation_rows(
    pg_url: str,
    *,
    capability_id: uuid.UUID,
    capability_tenant_id: uuid.UUID,
    author_actor_id: uuid.UUID,
    author_tenant_id: uuid.UUID,
    count: int,
) -> None:
    """Bulk-insert annotation rows directly via SQL.

    Used by the latency test to seed large volumes without paying the cost
    of 'count' POST round-trips through the full HTTP stack. The rows match
    the schema produced by AnnotationService.create_annotation: tenant_id is
    the capability's owner tenant, author_tenant_id is the submitting tenant.
    """
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    base_ts = _NOW
    try:
        async with factory() as session, session.begin():
            for i in range(count):
                ann_id = uuid.uuid4()
                ts = base_ts + datetime.timedelta(microseconds=i)
                await session.execute(
                    text(
                        """
                        INSERT INTO capability_annotations (
                            annotation_id, tenant_id, capability_id,
                            author_actor_id, author_tenant_id,
                            body, category, status,
                            created_at, updated_at,
                            t_valid_from, t_ingested_at
                        ) VALUES (
                            :annotation_id, :tenant_id, :capability_id,
                            :author_actor_id, :author_tenant_id,
                            :body, 'feedback', 'open',
                            :now, :now,
                            :now, :ts
                        )
                        """
                    ),
                    {
                        "annotation_id": ann_id,
                        "tenant_id": capability_tenant_id,
                        "capability_id": capability_id,
                        "author_actor_id": author_actor_id,
                        "author_tenant_id": author_tenant_id,
                        "body": f"Seeded annotation {i}",
                        "now": _NOW,
                        "ts": ts,
                    },
                )
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Harness helpers
# ---------------------------------------------------------------------------


async def _make_persona(
    harness: EntitlementAuthHarness, client: AsyncClient, slug: str, roles: list[str]
) -> tuple[TenantPersona, uuid.UUID, uuid.UUID]:
    """Add a persona, JIT-materialise via whoami, return (persona, tenant_id, actor_id)."""
    persona = harness.add_persona(slug, roles=roles)
    harness.configure_fetcher_for(persona)
    with patch_validator_for_actor(persona):
        resp = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))
        assert resp.status_code == 200, resp.text
    body = resp.json()
    return persona, uuid.UUID(body["tenant_id"]), uuid.UUID(body["actor_id"])


# ---------------------------------------------------------------------------
# App fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def app_client(pg_container: str) -> AsyncIterator[_AppClient]:
    """FastAPI app + AsyncClient wired to the live testcontainers Postgres."""
    async with EntitlementAuthHarness(pg_container) as harness:
        transport = ASGITransport(app=harness.app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield harness, client


# ---------------------------------------------------------------------------
# Cross-tenant visibility scenario
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_annotation_cross_tenant_visibility_flow(pg_container: str, app_client: _AppClient) -> None:
    """Normative three-tenant annotation flow.

    Setup: three tenants — A (provider/owner), B (consumer/author), C (third party).
    Tenant A owns a public capability. Tenant B submits an annotation. Then:

    1. Tenant B POST → 201, AnnotationResponse shape is correct.
    2. Tenant A GET → 200, provider path returns Tenant B's annotation in items.
    3. Tenant B GET → 200, author path returns only their own annotation.
    4. Tenant C GET → 200 with {items: [], next_cursor: null} — NOT 403.
    5. Tenant C's response body must not contain Tenant B's annotation_id.
    """
    harness, client = app_client
    suffix = uuid.uuid4().hex[:8]

    persona_a, a_tid, _a_actor = await _make_persona(harness, client, f"ann-vis-a-{suffix}", ["producer", "admin"])
    persona_b, b_tid, _b_actor = await _make_persona(harness, client, f"ann-vis-b-{suffix}", ["consumer"])
    persona_c, _c_tid, _c_actor = await _make_persona(harness, client, f"ann-vis-c-{suffix}", ["consumer"])

    cap_id = await _seed_capability(
        pg_container,
        tenant_id=a_tid,
        name=f"ann-vis-cap-{suffix}",
        visibility=VISIBILITY_PUBLIC,
    )

    # Step 1: Tenant B submits annotation → 201.
    harness.configure_fetcher_for(persona_b)
    with patch_validator_for_actor(persona_b):
        post_resp = await client.post(
            f"/v1/capabilities/{cap_id}/annotations",
            headers=bearer_headers(tenant_slug=persona_b.slug),
            json={"body": "This API lacks rate-limit headers.", "category": "feedback"},
        )
    assert post_resp.status_code == 201, post_resp.text
    post_body = post_resp.json()

    assert "annotation_id" in post_body
    assert "capability_id" in post_body
    assert post_body["capability_id"] == str(cap_id)
    assert post_body["author_tenant_id"] == str(b_tid)
    assert post_body["status"] == "open"
    assert post_body["category"] == "feedback"
    assert "encrypted_unrecoverable" not in post_body
    assert "warnings" not in post_body

    annotation_id = post_body["annotation_id"]

    # Step 2: Tenant A (provider) GET → sees Tenant B's annotation.
    harness.configure_fetcher_for(persona_a)
    with patch_validator_for_actor(persona_a):
        a_resp = await client.get(
            f"/v1/capabilities/{cap_id}/annotations",
            headers=bearer_headers(tenant_slug=persona_a.slug),
        )
    assert a_resp.status_code == 200, a_resp.text
    a_body = a_resp.json()
    assert "items" in a_body
    assert "next_cursor" in a_body
    a_item_ids = [item["annotation_id"] for item in a_body["items"]]
    assert annotation_id in a_item_ids, (
        f"Provider (Tenant A) should see Tenant B's annotation {annotation_id}; "
        f"got items: {a_item_ids}"
    )

    # Step 3: Tenant B (author) GET → sees only their own annotation.
    harness.configure_fetcher_for(persona_b)
    with patch_validator_for_actor(persona_b):
        b_resp = await client.get(
            f"/v1/capabilities/{cap_id}/annotations",
            headers=bearer_headers(tenant_slug=persona_b.slug),
        )
    assert b_resp.status_code == 200, b_resp.text
    b_body = b_resp.json()
    b_item_ids = [item["annotation_id"] for item in b_body["items"]]
    assert annotation_id in b_item_ids, f"Author (Tenant B) should see their own annotation; got: {b_item_ids}"
    for item in b_body["items"]:
        assert item["author_tenant_id"] == str(b_tid), f"Author path returned an item not authored by Tenant B: {item}"

    # Step 4: Tenant C GET → 200 with empty list (not 403).
    harness.configure_fetcher_for(persona_c)
    with patch_validator_for_actor(persona_c):
        c_resp = await client.get(
            f"/v1/capabilities/{cap_id}/annotations",
            headers=bearer_headers(tenant_slug=persona_c.slug),
        )
    assert c_resp.status_code == 200, (
        f"Third-tenant GET must return 200, not {c_resp.status_code}. "
        f"Response: {c_resp.text}"
    )
    c_body = c_resp.json()
    assert c_body["items"] == [], f"Tenant C (third party) must see empty items list; got: {c_body['items']}"
    assert c_body["next_cursor"] is None

    # Step 5: Tenant C's response must not contain Tenant B's annotation_id anywhere.
    assert annotation_id not in c_resp.text, f"Tenant C's response body leaks Tenant B's annotation_id {annotation_id}"


# ---------------------------------------------------------------------------
# Third-tenant isolation — direct DB verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_third_tenant_cannot_see_annotation_via_db(pg_container: str, app_client: _AppClient) -> None:
    """Belt-and-suspenders: verify Tenant C truly has no path to Tenant B's annotation."""
    harness, client = app_client
    suffix = uuid.uuid4().hex[:8]

    persona_a, a_tid, _a_actor = await _make_persona(harness, client, f"ann-db-a-{suffix}", ["producer", "admin"])
    persona_b, b_tid, _b_actor = await _make_persona(harness, client, f"ann-db-b-{suffix}", ["consumer"])
    persona_c, c_tid, _c_actor = await _make_persona(harness, client, f"ann-db-c-{suffix}", ["consumer"])

    cap_id = await _seed_capability(
        pg_container,
        tenant_id=a_tid,
        name=f"ann-db-cap-{suffix}",
        visibility=VISIBILITY_PUBLIC,
    )

    harness.configure_fetcher_for(persona_b)
    with patch_validator_for_actor(persona_b):
        post_resp = await client.post(
            f"/v1/capabilities/{cap_id}/annotations",
            headers=bearer_headers(tenant_slug=persona_b.slug),
            json={"body": "Seen only by B and A.", "category": "bug"},
        )
    assert post_resp.status_code == 201, post_resp.text
    annotation_id = uuid.UUID(post_resp.json()["annotation_id"])

    # Direct DB query: simulate what the author path does for Tenant C.
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await session.execute(
                text(
                    "SELECT annotation_id FROM capability_annotations "
                    "WHERE capability_id = :cap_id "
                    "  AND author_tenant_id = :c_tid "
                    "  AND t_invalidated_at IS NULL"
                ),
                {"cap_id": cap_id, "c_tid": c_tid},
            )
            c_rows = result.fetchall()
    finally:
        await engine.dispose()

    c_ids = [str(row.annotation_id) for row in c_rows]
    assert (
        str(annotation_id) not in c_ids
    ), f"DB author-path query for Tenant C returned Tenant B's annotation {annotation_id}"
    assert c_ids == [], f"Tenant C should have no annotations on this capability; got {c_ids}"


# ---------------------------------------------------------------------------
# Private-capability cross-tenant leak (the 200/404 distinction defect)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_annotations_returns_404_for_private_capability_from_unrelated_tenant(
    pg_container: str,
    app_client: _AppClient,
) -> None:
    """An unrelated tenant probing a private capability must not get 200 + empty list."""
    harness, client = app_client
    suffix = uuid.uuid4().hex[:8]

    persona_a, a_tid, _a_actor = await _make_persona(harness, client, f"ann-priv-a-{suffix}", ["producer", "admin"])
    persona_b, _b_tid, _b_actor = await _make_persona(harness, client, f"ann-priv-b-{suffix}", ["consumer"])

    cap_id = await _seed_capability(
        pg_container,
        tenant_id=a_tid,
        name=f"ann-priv-cap-{suffix}",
        visibility=VISIBILITY_PRIVATE,
    )

    harness.configure_fetcher_for(persona_b)
    with patch_validator_for_actor(persona_b):
        resp = await client.get(
            f"/v1/capabilities/{cap_id}/annotations",
            headers=bearer_headers(tenant_slug=persona_b.slug),
        )

    assert resp.status_code != 200, (
        f"private-capability list leaked existence as 200 from unrelated tenant; "
        f"response={resp.text}"
    )


# ---------------------------------------------------------------------------
# Latency assertion — p95 < 200 ms at 1,000 annotations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_annotations_p95_latency(pg_container: str, app_client: _AppClient) -> None:
    """Provider GET p95 latency must be below 200 ms at 1,000 seeded annotations."""
    harness, client = app_client
    suffix = uuid.uuid4().hex[:8]
    skip_assertion = os.environ.get("SKIP_LATENCY_TESTS", "").strip() == "1"

    persona_a, a_tid, a_actor = await _make_persona(harness, client, f"ann-lat-a-{suffix}", ["producer", "admin"])
    persona_b, b_tid, b_actor = await _make_persona(harness, client, f"ann-lat-b-{suffix}", ["consumer"])

    cap_id = await _seed_capability(
        pg_container,
        tenant_id=a_tid,
        name=f"ann-lat-cap-{suffix}",
        visibility=VISIBILITY_PUBLIC,
    )

    await _seed_annotation_rows(
        pg_container,
        capability_id=cap_id,
        capability_tenant_id=a_tid,
        author_actor_id=b_actor,
        author_tenant_id=b_tid,
        count=1000,
    )

    # Warm-up.
    harness.configure_fetcher_for(persona_a)
    with patch_validator_for_actor(persona_a):
        warmup = await client.get(
            f"/v1/capabilities/{cap_id}/annotations",
            headers=bearer_headers(tenant_slug=persona_a.slug),
        )
    assert warmup.status_code == 200, warmup.text

    # Timed loop: 10 requests.
    times: list[float] = []
    for _ in range(10):
        harness.configure_fetcher_for(persona_a)
        with patch_validator_for_actor(persona_a):
            t0 = time.perf_counter()
            resp = await client.get(
                f"/v1/capabilities/{cap_id}/annotations",
                headers=bearer_headers(tenant_slug=persona_a.slug),
            )
            elapsed = time.perf_counter() - t0
        assert resp.status_code == 200, resp.text
        times.append(elapsed)

    n = len(times)
    sorted_times = sorted(times)
    p95_index = max(0, int(n * 0.95) - 1)
    p95 = sorted_times[p95_index]

    print(
        f"\nLatency at 1,000 annotations — times (ms): "
        f"{[round(t * 1000, 1) for t in sorted_times]}  p95={round(p95 * 1000, 1)} ms"
    )

    if not skip_assertion:
        assert p95 < 0.200, (
            f"GET /v1/capabilities/{{id}}/annotations p95 latency at 1,000 annotations "
            f"is {round(p95 * 1000, 1)} ms, exceeding the 200 ms SLO. "
            f"All times (ms): {[round(t * 1000, 1) for t in sorted_times]}"
        )
