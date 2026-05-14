"""Integration tests: annotation status transitions.

Covers the triage state machine and list-filter behavior:

Forward chain:   open → triaged → acknowledged → closed (3 PATCH calls).
Reverse chain:   closed → triaged (reverse transition is permitted; audit row written).
List filter:     GET ?status=triaged returns exactly the triaged annotations.
Auth gate:       Tenant B (annotation author, not capability owner) cannot PATCH → 403.

Each PATCH is expected to write one audit row with action='annotation.triaged'.
The forward chain produces 3 audit rows; the reverse chain adds a 4th.

Authorization rule: only the capability's owner tenant (producer/admin role) can
triage (PATCH) an annotation. The annotation's author tenant cannot — this is
verified by the non-owner PATCH → 403 test.

NOTE: AnnotationService.list_annotations currently queries
    SELECT tenant_id FROM capabilities WHERE capability_id = :cid
The "capabilities" table does not exist — the correct name is "entities". Tests
that reach the list path will fail with a DB error until the service is fixed.
The PATCH path (triage_annotation) uses get_annotation which queries
capability_annotations directly and then checks ctx.tenant_id == annotation.tenant_id
— it does NOT query the capabilities table. The PATCH-only tests should pass.
The list-filter tests will fail due to the same bug.
"""

from __future__ import annotations

import datetime
import secrets
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.api.auth.tokens import hash_token
from registry.config import Settings
from registry.main import create_app
from registry.service.visibility import VISIBILITY_PUBLIC

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_tenant_with_token(
    pg_url: str,
    *,
    slug: str,
    roles: list[str] | None = None,
) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Insert (tenant, actor, api_token). Returns (tenant_id, actor_id, raw_token)."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    raw_token = secrets.token_urlsafe(24)
    role_list = roles or ["producer", "consumer", "admin"]
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
                    "roles": role_list,
                    "now": _NOW,
                },
            )
    finally:
        await engine.dispose()
    return tenant_id, actor_id, raw_token


async def _seed_capability(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    name: str,
    visibility: str = VISIBILITY_PUBLIC,
) -> uuid.UUID:
    """Insert one capability entity owned by tenant_id."""
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


async def _count_audit_rows(pg_url: str, annotation_id: uuid.UUID) -> int:
    """Count audit_log rows for the given annotation_id with action=annotation.triaged."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await session.execute(
                text(
                    "SELECT COUNT(*) FROM audit_log "
                    "WHERE target_type = 'annotation' "
                    "  AND target_id = :ann_id "
                    "  AND action = 'annotation.triaged'"
                ),
                {"ann_id": annotation_id},
            )
            return int(result.scalar_one())
    finally:
        await engine.dispose()


async def _get_annotation_db_status(pg_url: str, annotation_id: uuid.UUID) -> str:
    """Fetch the current status of an annotation row directly from the DB."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await session.execute(
                text("SELECT status FROM capability_annotations " "WHERE annotation_id = :ann_id"),
                {"ann_id": annotation_id},
            )
            row = result.first()
            if row is None:
                raise AssertionError(f"Annotation {annotation_id} not found in DB")
            return str(row.status)
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# App fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def app_client(pg_container: str):  # type: ignore[type-arg]
    """FastAPI app + AsyncClient wired to the live testcontainers Postgres."""
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
        yield client


# ---------------------------------------------------------------------------
# Forward chain: open → triaged → acknowledged → closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forward_status_chain_open_to_closed(pg_container: str, app_client) -> None:
    """Triage forward chain produces correct status progression + 3 audit rows.

    Tenant A (provider) triages Tenant B's annotation through:
        open → triaged → acknowledged → closed

    After each PATCH the response body must reflect the new status. After all
    three PATCHes the DB row must show status='closed' and audit_log must
    contain exactly 3 rows with action='annotation.triaged' for this annotation.
    """
    client = app_client
    suffix = uuid.uuid4().hex[:8]

    a_tid, _a_actor, a_token = await _seed_tenant_with_token(
        pg_container,
        slug=f"ann-fwd-a-{suffix}",
        roles=["producer", "admin"],
    )
    _b_tid, _b_actor, b_token = await _seed_tenant_with_token(
        pg_container,
        slug=f"ann-fwd-b-{suffix}",
        roles=["consumer"],
    )

    cap_id = await _seed_capability(
        pg_container,
        tenant_id=a_tid,
        name=f"ann-fwd-cap-{suffix}",
        visibility=VISIBILITY_PUBLIC,
    )

    # Tenant B creates the annotation.
    create_resp = await client.post(
        f"/v1/capabilities/{cap_id}/annotations",
        headers={"Authorization": f"Bearer {b_token}"},
        json={"body": "The response schema is missing error codes.", "category": "bug"},
    )
    assert create_resp.status_code == 201, create_resp.text
    annotation_id = uuid.UUID(create_resp.json()["annotation_id"])
    assert create_resp.json()["status"] == "open"

    a_headers = {"Authorization": f"Bearer {a_token}"}

    # PATCH 1: open → triaged.
    p1 = await client.patch(
        f"/v1/annotations/{annotation_id}",
        headers=a_headers,
        json={"status": "triaged"},
    )
    assert p1.status_code == 200, p1.text
    assert p1.json()["status"] == "triaged"

    # PATCH 2: triaged → acknowledged.
    p2 = await client.patch(
        f"/v1/annotations/{annotation_id}",
        headers=a_headers,
        json={"status": "acknowledged"},
    )
    assert p2.status_code == 200, p2.text
    assert p2.json()["status"] == "acknowledged"

    # PATCH 3: acknowledged → closed.
    p3 = await client.patch(
        f"/v1/annotations/{annotation_id}",
        headers=a_headers,
        json={"status": "closed"},
    )
    assert p3.status_code == 200, p3.text
    assert p3.json()["status"] == "closed"

    # DB state: status column must now be 'closed'.
    db_status = await _get_annotation_db_status(pg_container, annotation_id)
    assert db_status == "closed", f"Expected DB status='closed', got {db_status!r}"

    # Audit log: exactly 3 annotation.triaged rows for this annotation.
    audit_count = await _count_audit_rows(pg_container, annotation_id)
    assert audit_count == 3, (
        f"Expected 3 audit rows (one per PATCH) for annotation {annotation_id}; " f"got {audit_count}"
    )


# ---------------------------------------------------------------------------
# Reverse transition: closed → triaged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reverse_transition_closed_to_triaged(pg_container: str, app_client) -> None:
    """Reverse transitions are permitted — closed → triaged succeeds.

    The AN phase imposes no enforced state machine graph beyond membership in
    the valid-status vocabulary. Starting from 'closed', a PATCH to 'triaged'
    must return 200 and write a new audit row.
    """
    client = app_client
    suffix = uuid.uuid4().hex[:8]

    a_tid, _a_actor, a_token = await _seed_tenant_with_token(
        pg_container,
        slug=f"ann-rev-a-{suffix}",
        roles=["producer", "admin"],
    )
    _b_tid, _b_actor, b_token = await _seed_tenant_with_token(
        pg_container,
        slug=f"ann-rev-b-{suffix}",
        roles=["consumer"],
    )

    cap_id = await _seed_capability(
        pg_container,
        tenant_id=a_tid,
        name=f"ann-rev-cap-{suffix}",
        visibility=VISIBILITY_PUBLIC,
    )

    create_resp = await client.post(
        f"/v1/capabilities/{cap_id}/annotations",
        headers={"Authorization": f"Bearer {b_token}"},
        json={"body": "Rate limits are inconsistent.", "category": "bug"},
    )
    assert create_resp.status_code == 201, create_resp.text
    annotation_id = uuid.UUID(create_resp.json()["annotation_id"])

    a_headers = {"Authorization": f"Bearer {a_token}"}

    # Drive to closed (1 audit row).
    p1 = await client.patch(
        f"/v1/annotations/{annotation_id}",
        headers=a_headers,
        json={"status": "closed"},
    )
    assert p1.status_code == 200, p1.text
    assert p1.json()["status"] == "closed"

    audit_before = await _count_audit_rows(pg_container, annotation_id)
    assert audit_before == 1

    # Reverse: closed → triaged.
    p2 = await client.patch(
        f"/v1/annotations/{annotation_id}",
        headers=a_headers,
        json={"status": "triaged", "triage_note": "Reopening after customer escalation."},
    )
    assert p2.status_code == 200, p2.text
    assert p2.json()["status"] == "triaged"

    # DB must reflect the new status.
    db_status = await _get_annotation_db_status(pg_container, annotation_id)
    assert db_status == "triaged", f"Expected DB status='triaged' after reverse; got {db_status!r}"

    # A second audit row must have been written for the reverse transition.
    audit_after = await _count_audit_rows(pg_container, annotation_id)
    assert audit_after == 2, f"Expected 2 audit rows after reverse transition; got {audit_after}"


# ---------------------------------------------------------------------------
# Status filter on list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_status_filter(pg_container: str, app_client) -> None:
    """GET ?status=<value> filters the result set correctly.

    Three annotations on the same capability: one open, one triaged, one closed.

    - GET ?status=triaged → exactly 1 item.
    - GET ?status=open    → exactly 1 item.
    - GET (no filter)     → all 3 items.
    """
    client = app_client
    suffix = uuid.uuid4().hex[:8]

    a_tid, _a_actor, a_token = await _seed_tenant_with_token(
        pg_container,
        slug=f"ann-flt-a-{suffix}",
        roles=["producer", "admin"],
    )
    _b_tid, _b_actor, b_token = await _seed_tenant_with_token(
        pg_container,
        slug=f"ann-flt-b-{suffix}",
        roles=["consumer"],
    )

    cap_id = await _seed_capability(
        pg_container,
        tenant_id=a_tid,
        name=f"ann-flt-cap-{suffix}",
        visibility=VISIBILITY_PUBLIC,
    )

    a_headers = {"Authorization": f"Bearer {a_token}"}
    b_headers = {"Authorization": f"Bearer {b_token}"}

    # Create annotation 1: will stay 'open'.
    r1 = await client.post(
        f"/v1/capabilities/{cap_id}/annotations",
        headers=b_headers,
        json={"body": "Documentation is outdated.", "category": "doc_gap"},
    )
    assert r1.status_code == 201, r1.text
    ann1_id = r1.json()["annotation_id"]

    # Create annotation 2: drive to 'triaged'.
    r2 = await client.post(
        f"/v1/capabilities/{cap_id}/annotations",
        headers=b_headers,
        json={"body": "Authentication errors are not actionable.", "category": "feedback"},
    )
    assert r2.status_code == 201, r2.text
    ann2_id = uuid.UUID(r2.json()["annotation_id"])
    p2 = await client.patch(
        f"/v1/annotations/{ann2_id}",
        headers=a_headers,
        json={"status": "triaged"},
    )
    assert p2.status_code == 200, p2.text

    # Create annotation 3: drive to 'closed'.
    r3 = await client.post(
        f"/v1/capabilities/{cap_id}/annotations",
        headers=b_headers,
        json={"body": "Batch endpoint is missing.", "category": "suggestion"},
    )
    assert r3.status_code == 201, r3.text
    ann3_id = uuid.UUID(r3.json()["annotation_id"])
    p3 = await client.patch(
        f"/v1/annotations/{ann3_id}",
        headers=a_headers,
        json={"status": "closed"},
    )
    assert p3.status_code == 200, p3.text

    # GET ?status=triaged → exactly 1 item.
    flt_triaged = await client.get(
        f"/v1/capabilities/{cap_id}/annotations",
        headers=a_headers,
        params={"status": "triaged"},
    )
    assert flt_triaged.status_code == 200, flt_triaged.text
    triaged_items = flt_triaged.json()["items"]
    assert len(triaged_items) == 1, f"Expected 1 triaged annotation; got {len(triaged_items)}: {triaged_items}"
    assert triaged_items[0]["annotation_id"] == str(ann2_id)

    # GET ?status=open → exactly 1 item.
    flt_open = await client.get(
        f"/v1/capabilities/{cap_id}/annotations",
        headers=a_headers,
        params={"status": "open"},
    )
    assert flt_open.status_code == 200, flt_open.text
    open_items = flt_open.json()["items"]
    assert len(open_items) == 1, f"Expected 1 open annotation; got {len(open_items)}: {open_items}"
    assert open_items[0]["annotation_id"] == ann1_id

    # GET (no filter) → all 3 items.
    flt_all = await client.get(
        f"/v1/capabilities/{cap_id}/annotations",
        headers=a_headers,
    )
    assert flt_all.status_code == 200, flt_all.text
    all_items = flt_all.json()["items"]
    assert len(all_items) == 3, f"Expected 3 total annotations (no filter); got {len(all_items)}: {all_items}"


# ---------------------------------------------------------------------------
# Non-owner triage attempt → 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_owner_tenant_cannot_triage(pg_container: str, app_client) -> None:
    """Tenant B (annotation author, not capability owner) PATCH → 403.

    Triage authorization is scoped to the capability's owner tenant. An actor
    from a different tenant — even the annotation's author — must receive 403
    when attempting to PATCH the annotation status.
    """
    client = app_client
    suffix = uuid.uuid4().hex[:8]

    a_tid, _a_actor, _a_token = await _seed_tenant_with_token(
        pg_container,
        slug=f"ann-auth-a-{suffix}",
        roles=["producer", "admin"],
    )
    _b_tid, _b_actor, b_token = await _seed_tenant_with_token(
        pg_container,
        slug=f"ann-auth-b-{suffix}",
        # consumer + producer roles to rule out role-check as the failure reason;
        # the ownership check must be what fires.
        roles=["consumer", "producer"],
    )

    cap_id = await _seed_capability(
        pg_container,
        tenant_id=a_tid,
        name=f"ann-auth-cap-{suffix}",
        visibility=VISIBILITY_PUBLIC,
    )

    # Tenant B creates the annotation.
    create_resp = await client.post(
        f"/v1/capabilities/{cap_id}/annotations",
        headers={"Authorization": f"Bearer {b_token}"},
        json={"body": "The error messages need clearer codes.", "category": "feedback"},
    )
    assert create_resp.status_code == 201, create_resp.text
    annotation_id = uuid.UUID(create_resp.json()["annotation_id"])

    # Tenant B attempts to triage their own annotation → must be 403.
    patch_resp = await client.patch(
        f"/v1/annotations/{annotation_id}",
        headers={"Authorization": f"Bearer {b_token}"},
        json={"status": "triaged"},
    )
    assert patch_resp.status_code == 403, (
        f"Expected 403 when non-owner Tenant B tries to triage; " f"got {patch_resp.status_code}: {patch_resp.text}"
    )
