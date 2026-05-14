"""Annotation invariant conformance gates.

Three contract-drift guards that must hold before the annotations phase is
considered shippable:

1. PII scan chokepoint — every write path (create_annotation,
   triage_annotation with a note) invokes the PII scanner before any DB
   write, and a scanner that raises propagates the failure to the HTTP
   response without writing a row.

2. Status state machine — all four status values (open, triaged,
   acknowledged, closed) are reachable from 'open' via PATCH, and a
   reverse transition (closed → triaged) also succeeds.

3. assert_visible call-count — VisibilityService.assert_visible is called
   exactly once per create_annotation invocation, proving there is no
   fast-path that bypasses the chokepoint.

These tests use real Postgres (testcontainers) and the live FastAPI app via
httpx.ASGITransport.  The pg_container and app_settings fixtures come from
the root-level tests/conftest.py.
"""

from __future__ import annotations

import datetime
import secrets
import uuid
from typing import Any

import pytest
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
    """Insert one capability entity owned by tenant_id. Returns entity_id."""
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


async def _count_annotations(
    pg_url: str,
    *,
    capability_id: uuid.UUID,
) -> int:
    """Count active (non-soft-deleted) annotation rows for a capability."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await session.execute(
                text(
                    "SELECT COUNT(*) FROM capability_annotations "
                    "WHERE capability_id = :cid AND t_invalidated_at IS NULL"
                ),
                {"cid": capability_id},
            )
            return result.scalar_one()
    finally:
        await engine.dispose()


async def _fetch_annotation_status(
    pg_url: str,
    *,
    annotation_id: uuid.UUID,
) -> str | None:
    """Return the current status of an annotation row, or None if not found."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await session.execute(
                text("SELECT status FROM capability_annotations " "WHERE annotation_id = :aid"),
                {"aid": annotation_id},
            )
            row = result.fetchone()
            return row[0] if row else None
    finally:
        await engine.dispose()


async def _fetch_audit_rows_for_annotation(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    annotation_id: uuid.UUID,
    action: str,
) -> list[dict[str, Any]]:
    """Fetch audit_log rows for a given annotation_id and action, newest first."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await session.execute(
                text(
                    "SELECT audit_id, action, after_jsonb, ts "
                    "FROM audit_log "
                    "WHERE tenant_id = :tid "
                    "  AND action = :action "
                    "  AND after_jsonb->>'annotation_id' = :aid "
                    "ORDER BY ts DESC"
                ),
                {
                    "tid": tenant_id,
                    "action": action,
                    "aid": str(annotation_id),
                },
            )
            return [dict(r._mapping) for r in result.fetchall()]
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Failing PII scanner stub
# ---------------------------------------------------------------------------


class _AlwaysBombScanner:
    """PIIScanner stub whose scan() always raises RuntimeError.

    Injected into app.state.pii_scanner before the request so the HTTP handler
    receives the exception.  Every write path that reaches the PII chokepoint
    will fail immediately — no DB row must be written.
    """

    def scan(self, text: str, *, field_type: str, **_kwargs: Any) -> Any:
        raise RuntimeError("_AlwaysBombScanner: unconditional PII scanner failure")


# ---------------------------------------------------------------------------
# Invariant 1 — PII scan chokepoint cannot be bypassed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pii_chokepoint_blocks_create(
    pg_container: str,
    app_settings: Settings,
) -> None:
    """A failing PII scanner must prevent INSERT on POST /v1/capabilities/{id}/annotations.

    The scanner is injected via app.state.pii_scanner before the request.
    The scanner raises unconditionally, so the HTTP response must be a 5xx
    (the exception propagates from the service layer through FastAPI's
    unhandled-exception handler).  After the failure, zero annotation rows
    must exist for the capability — the partial-index count must not grow.
    """
    suffix = uuid.uuid4().hex[:6]
    tenant_id, _actor_id, token = await _seed_tenant_with_token(pg_container, slug=f"pii-create-{suffix}")
    cap_id = await _seed_capability(pg_container, tenant_id=tenant_id, name=f"cap-pii-create-{suffix}")

    before_count = await _count_annotations(pg_container, capability_id=cap_id)

    app = create_app(app_settings)
    # AnnotationService is built once at app startup with a captured pii_scanner
    # reference (singleton — see registry/api/routers/annotations.py::
    # _build_annotation_service). Setting app.state.pii_scanner after
    # create_app() has no effect on the singleton, so inject the bomb scanner
    # directly into the existing instance.
    app.state.annotation_service._pii_scanner = _AlwaysBombScanner()

    # raise_app_exceptions=False lets the transport catch the unhandled
    # RuntimeError and return a 500 response body rather than re-raising it
    # into the test — which is the observable HTTP contract we want to verify.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/v1/capabilities/{cap_id}/annotations",
            json={"body": "This is a test annotation.", "category": "feedback"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code >= 500, f"Expected 5xx when PII scanner raises; got {resp.status_code}: {resp.text}"

    after_count = await _count_annotations(pg_container, capability_id=cap_id)
    assert after_count == before_count, (
        f"No annotation rows must be written when PII scanner raises; " f"before={before_count} after={after_count}"
    )


@pytest.mark.asyncio
async def test_pii_chokepoint_blocks_triage_note(
    pg_container: str,
    app_settings: Settings,
) -> None:
    """A failing PII scanner must prevent UPDATE when PATCH supplies a triage_note.

    Setup: create one annotation via a healthy scanner, then inject the
    failing scanner and PATCH with a triage_note.  The PATCH must fail with
    5xx and the annotation's status must be unchanged in the DB.
    """
    suffix = uuid.uuid4().hex[:6]
    tenant_id, _actor_id, token = await _seed_tenant_with_token(pg_container, slug=f"pii-triage-{suffix}")
    cap_id = await _seed_capability(pg_container, tenant_id=tenant_id, name=f"cap-pii-triage-{suffix}")

    # Step 1 — create annotation with healthy scanner.
    healthy_app = create_app(app_settings)
    transport = ASGITransport(app=healthy_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            f"/v1/capabilities/{cap_id}/annotations",
            json={"body": "Initial annotation body.", "category": "bug"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert (
        create_resp.status_code == 201
    ), f"Annotation creation should succeed; got {create_resp.status_code}: {create_resp.text}"
    annotation_id = uuid.UUID(create_resp.json()["annotation_id"])

    status_before = await _fetch_annotation_status(pg_container, annotation_id=annotation_id)
    assert status_before == "open"

    # Step 2 — inject failing scanner and PATCH with triage_note. The
    # AnnotationService singleton captures pii_scanner at app startup, so
    # mutate the instance's _pii_scanner directly rather than setting
    # app.state.pii_scanner (which has no effect post-startup).
    failing_app = create_app(app_settings)
    failing_app.state.annotation_service._pii_scanner = _AlwaysBombScanner()

    # raise_app_exceptions=False converts the unhandled RuntimeError into a
    # 500 response rather than re-raising it — the contract being verified is
    # that the HTTP layer fails before any DB write, not which Python exception
    # propagated.
    transport2 = ASGITransport(app=failing_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport2, base_url="http://test") as client:
        patch_resp = await client.patch(
            f"/v1/annotations/{annotation_id}",
            json={"status": "triaged", "triage_note": "Some triage note text."},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert patch_resp.status_code >= 500, (
        f"Expected 5xx when PII scanner raises on triage_note; " f"got {patch_resp.status_code}: {patch_resp.text}"
    )

    status_after = await _fetch_annotation_status(pg_container, annotation_id=annotation_id)
    assert status_after == status_before, (
        f"Annotation status must be unchanged when PII scanner raises; "
        f"before={status_before!r} after={status_after!r}"
    )


# ---------------------------------------------------------------------------
# Invariant 2 — Status state machine: all four values reachable from 'open'
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_state_machine_all_reachable(
    pg_container: str,
    app_settings: Settings,
) -> None:
    """All four status values are reachable from 'open', and reverse transitions work.

    Walk: open → triaged → acknowledged → closed → triaged (reverse).
    Each step asserts 200 OK on the HTTP PATCH.  After the full walk, the DB
    row must hold status='triaged'.

    The audit_log is queried after each PATCH to confirm an annotation.triaged
    row was written for every real transition (self-transitions are no-ops and
    emit no audit entry — this test does not issue any self-transition).

    Expected triage audit rows across the full walk: 4 (one per real transition,
    including the reverse).
    """
    suffix = uuid.uuid4().hex[:6]
    # Tenant A owns the capability and performs all triage operations.
    tenant_a_id, _actor_a, token_a = await _seed_tenant_with_token(
        pg_container, slug=f"sm-a-{suffix}", roles=["producer", "admin"]
    )
    # Tenant B submits the annotation (different tenant = consumer flow).
    _tenant_b_id, _actor_b, token_b = await _seed_tenant_with_token(
        pg_container, slug=f"sm-b-{suffix}", roles=["consumer", "producer", "admin"]
    )
    cap_id = await _seed_capability(pg_container, tenant_id=tenant_a_id, name=f"cap-sm-{suffix}")

    app = create_app(app_settings)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Tenant B creates the annotation → status=open.
        create_resp = await client.post(
            f"/v1/capabilities/{cap_id}/annotations",
            json={"body": "State machine test annotation.", "category": "suggestion"},
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert (
            create_resp.status_code == 201
        ), f"Annotation creation must succeed; got {create_resp.status_code}: {create_resp.text}"
        annotation_id = uuid.UUID(create_resp.json()["annotation_id"])

        # Forward walk: open → triaged → acknowledged → closed.
        for target_status in ("triaged", "acknowledged", "closed"):
            resp = await client.patch(
                f"/v1/annotations/{annotation_id}",
                json={"status": target_status},
                headers={"Authorization": f"Bearer {token_a}"},
            )
            assert resp.status_code == 200, (
                f"PATCH to status={target_status!r} must return 200; " f"got {resp.status_code}: {resp.text}"
            )
            assert resp.json()["status"] == target_status

        # Verify DB row after forward walk.
        db_status = await _fetch_annotation_status(pg_container, annotation_id=annotation_id)
        assert db_status == "closed", f"DB must show status='closed' after forward walk; got {db_status!r}"

        # Reverse transition: closed → triaged.
        reverse_resp = await client.patch(
            f"/v1/annotations/{annotation_id}",
            json={"status": "triaged"},
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert reverse_resp.status_code == 200, (
            f"Reverse transition closed → triaged must return 200; "
            f"got {reverse_resp.status_code}: {reverse_resp.text}"
        )
        assert reverse_resp.json()["status"] == "triaged"

    # Verify DB row after reverse transition.
    db_status_final = await _fetch_annotation_status(pg_container, annotation_id=annotation_id)
    assert (
        db_status_final == "triaged"
    ), f"DB must show status='triaged' after reverse transition; got {db_status_final!r}"

    # Audit log must have 4 annotation.triaged rows (3 forward + 1 reverse).
    # The initial open→triaged step is the first triage event.
    triage_rows = await _fetch_audit_rows_for_annotation(
        pg_container,
        tenant_id=tenant_a_id,
        annotation_id=annotation_id,
        action="annotation.triaged",
    )
    assert len(triage_rows) == 4, (
        f"Expected 4 annotation.triaged audit rows (3 forward + 1 reverse); " f"got {len(triage_rows)}"
    )


# ---------------------------------------------------------------------------
# Invariant 3 — assert_visible called exactly once per create_annotation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assert_visible_invoked_per_create(
    pg_container: str,
    app_settings: Settings,
) -> None:
    """VisibilityService.assert_visible is called exactly once per create_annotation call.

    Wraps the VisibilityService instance's assert_visible method on app.state
    with a thin counting shim (preserving the original coroutine).  Posts two
    annotations sequentially.  After the first call the counter must be 1;
    after the second it must be 2.  This proves there is no fast-path that
    bypasses the visibility chokepoint.
    """
    from registry.service.visibility import VisibilityService  # noqa: PLC0415

    suffix = uuid.uuid4().hex[:6]
    tenant_id, _actor_id, token = await _seed_tenant_with_token(pg_container, slug=f"vis-count-{suffix}")
    cap_id = await _seed_capability(pg_container, tenant_id=tenant_id, name=f"cap-vis-count-{suffix}")

    app = create_app(app_settings)

    # Retrieve the shared VisibilityService instance from app.state and wrap
    # its assert_visible method with a counting shim.  The shim delegates to the
    # original coroutine so real visibility enforcement is preserved.
    vis_svc: VisibilityService = app.state.visibility
    call_count = 0
    _original = vis_svc.assert_visible

    async def _counting_assert_visible(ctx: Any, entity_id: Any) -> None:
        nonlocal call_count
        call_count += 1
        return await _original(ctx, entity_id)

    vis_svc.assert_visible = _counting_assert_visible  # type: ignore[method-assign]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # First annotation — counter must reach 1.
        resp1 = await client.post(
            f"/v1/capabilities/{cap_id}/annotations",
            json={"body": "Visibility check annotation one.", "category": "feedback"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp1.status_code == 201, f"First annotation must succeed; got {resp1.status_code}: {resp1.text}"
        assert call_count == 1, (
            f"assert_visible must be called exactly once after first create; " f"got call_count={call_count}"
        )

        # Second annotation — counter must reach 2.
        resp2 = await client.post(
            f"/v1/capabilities/{cap_id}/annotations",
            json={"body": "Visibility check annotation two.", "category": "question"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp2.status_code == 201, f"Second annotation must succeed; got {resp2.status_code}: {resp2.text}"
        assert call_count == 2, (
            f"assert_visible must be called exactly once per create; "
            f"got call_count={call_count} after second create"
        )
