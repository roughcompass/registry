"""Annotation invariant conformance gates.

Three contract-drift guards that must hold before the annotation
service is considered shippable:

1. PII scan chokepoint — every write path (POST annotations, PATCH
   /annotations/{id} with a triage_note) invokes the PII scanner
   before any DB write; a scanner that raises propagates the failure
   to the HTTP response without writing a row.
2. Status state machine — all four status values (open, triaged,
   acknowledged, closed) are reachable from 'open' via PATCH, and a
   reverse transition (closed → triaged) also succeeds.
3. assert_visible call-count — VisibilityService.assert_visible is
   called exactly once per create_annotation invocation; no fast-path
   bypasses the chokepoint.

Auth is driven by tests/helpers/auth_harness.py: the OIDC validator
is patched and the entitlement resolver's fetcher is mocked.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    TenantPersona,
    bearer_headers,
    patch_validator_for_actor,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def harness(pg_container: str) -> AsyncIterator[EntitlementAuthHarness]:
    async with EntitlementAuthHarness(pg_container) as h:
        yield h


async def _materialise(
    h: EntitlementAuthHarness, client: AsyncClient, persona: TenantPersona
) -> None:
    """JIT-create the persona's tenant + actor by hitting /v1/whoami."""
    h.configure_fetcher_for(persona)
    with patch_validator_for_actor(persona):
        resp = await client.get(
            "/v1/whoami", headers=bearer_headers(tenant_slug=persona.slug)
        )
        assert resp.status_code == 200, resp.text


async def _seed_vocabulary(pg_url: str, slug: str) -> None:
    engine = create_async_engine(
        pg_url, connect_args={"prepared_statement_cache_size": 0}
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            row = (
                await session.execute(
                    text("SELECT tenant_id FROM tenants WHERE slug = :slug"),
                    {"slug": slug},
                )
            ).first()
            assert row is not None
            tenant_id = row[0]
            for kind, value in [
                ("entity_type", "capability"),
                ("fact_category", "overview"),
            ]:
                await session.execute(
                    text(
                        "INSERT INTO vocabulary_values (tenant_id, kind, value, is_system) "
                        "VALUES (:tid, :kind, :value, FALSE) ON CONFLICT DO NOTHING"
                    ),
                    {"tid": tenant_id, "kind": kind, "value": value},
                )
    finally:
        await engine.dispose()


async def _create_capability(
    h: EntitlementAuthHarness, client: AsyncClient, persona: TenantPersona, *, name: str
) -> uuid.UUID:
    h.configure_fetcher_for(persona)
    with patch_validator_for_actor(persona):
        resp = await client.post(
            "/v1/capabilities",
            json={"name": name},
            headers=bearer_headers(tenant_slug=persona.slug),
        )
    assert resp.status_code == 201, f"capability create failed: {resp.status_code} {resp.text}"
    return uuid.UUID(resp.json()["entity_id"])


async def _count_annotations(pg_url: str, *, capability_id: uuid.UUID) -> int:
    engine = create_async_engine(
        pg_url, connect_args={"prepared_statement_cache_size": 0}
    )
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
            return int(result.scalar_one())
    finally:
        await engine.dispose()


async def _fetch_annotation_status(pg_url: str, *, annotation_id: uuid.UUID) -> str | None:
    engine = create_async_engine(
        pg_url, connect_args={"prepared_statement_cache_size": 0}
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await session.execute(
                text(
                    "SELECT status FROM capability_annotations "
                    "WHERE annotation_id = :aid AND t_invalidated_at IS NULL"
                ),
                {"aid": annotation_id},
            )
            row = result.fetchone()
            return row[0] if row else None
    finally:
        await engine.dispose()


async def _count_audit_rows(
    pg_url: str, *, tenant_id: uuid.UUID, action: str, annotation_id: uuid.UUID
) -> int:
    engine = create_async_engine(
        pg_url, connect_args={"prepared_statement_cache_size": 0}
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await session.execute(
                text(
                    "SELECT COUNT(*) FROM audit_log "
                    "WHERE tenant_id = :tid AND action = :action "
                    "  AND after_jsonb->>'annotation_id' = :aid"
                ),
                {"tid": tenant_id, "action": action, "aid": str(annotation_id)},
            )
            return int(result.scalar_one())
    finally:
        await engine.dispose()


async def _lookup_tenant_id(pg_url: str, slug: str) -> uuid.UUID:
    engine = create_async_engine(
        pg_url, connect_args={"prepared_statement_cache_size": 0}
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            row = (
                await session.execute(
                    text("SELECT tenant_id FROM tenants WHERE slug = :slug"),
                    {"slug": slug},
                )
            ).first()
        assert row is not None
        return uuid.UUID(str(row[0]))
    finally:
        await engine.dispose()


class _AlwaysBombScanner:
    """PII scanner stub whose scan() always raises RuntimeError.

    Injected onto app.state.annotation_service._pii_scanner before the
    write request. Every chokepoint that calls scan() must propagate
    the failure to the HTTP response without writing a row.
    """

    def scan(self, text: str, *, field_type: str, **_kwargs: Any) -> Any:
        raise RuntimeError("_AlwaysBombScanner: unconditional PII scanner failure")


# ---------------------------------------------------------------------------
# Invariant 1 — PII scan chokepoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pii_chokepoint_blocks_create(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    """Failing PII scanner must prevent INSERT on POST annotations."""
    suffix = uuid.uuid4().hex[:6]
    persona = harness.add_persona(
        f"pii-create-{suffix}", roles=["producer", "consumer"]
    )
    transport = ASGITransport(app=harness.app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _materialise(harness, client, persona)
        await _seed_vocabulary(pg_container, persona.slug)
        cap_id = await _create_capability(
            harness, client, persona, name=f"cap-pii-create-{suffix}"
        )
        before = await _count_annotations(pg_container, capability_id=cap_id)

        harness.app.state.annotation_service._pii_scanner = _AlwaysBombScanner()
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            resp = await client.post(
                f"/v1/capabilities/{cap_id}/annotations",
                json={"body": "This is a test annotation.", "category": "feedback"},
                headers=bearer_headers(tenant_slug=persona.slug),
            )
    assert resp.status_code >= 500, (
        f"Expected 5xx when PII scanner raises; got {resp.status_code}: {resp.text}"
    )
    after = await _count_annotations(pg_container, capability_id=cap_id)
    assert after == before, (
        f"No annotation rows must be written; before={before} after={after}"
    )


@pytest.mark.asyncio
async def test_pii_chokepoint_blocks_triage_note(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    """Failing PII scanner must prevent UPDATE when PATCH supplies a triage_note."""
    suffix = uuid.uuid4().hex[:6]
    persona = harness.add_persona(
        f"pii-triage-{suffix}", roles=["producer", "consumer", "admin"]
    )
    transport = ASGITransport(app=harness.app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _materialise(harness, client, persona)
        await _seed_vocabulary(pg_container, persona.slug)
        cap_id = await _create_capability(
            harness, client, persona, name=f"cap-pii-triage-{suffix}"
        )

        # Step 1 — create with the healthy scanner.
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            create_resp = await client.post(
                f"/v1/capabilities/{cap_id}/annotations",
                json={"body": "Initial annotation body.", "category": "bug"},
                headers=bearer_headers(tenant_slug=persona.slug),
            )
        assert create_resp.status_code == 201, create_resp.text
        annotation_id = uuid.UUID(create_resp.json()["annotation_id"])
        status_before = await _fetch_annotation_status(
            pg_container, annotation_id=annotation_id
        )
        assert status_before == "open"

        # Step 2 — bomb the scanner and PATCH with a triage_note.
        harness.app.state.annotation_service._pii_scanner = _AlwaysBombScanner()
        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            patch_resp = await client.patch(
                f"/v1/annotations/{annotation_id}",
                json={"status": "triaged", "triage_note": "Some triage note text."},
                headers=bearer_headers(tenant_slug=persona.slug),
            )
    assert patch_resp.status_code >= 500, (
        f"Expected 5xx on triage_note PATCH; got {patch_resp.status_code}: {patch_resp.text}"
    )
    status_after = await _fetch_annotation_status(
        pg_container, annotation_id=annotation_id
    )
    assert status_after == status_before, (
        f"Status must be unchanged after failed PATCH; "
        f"before={status_before!r} after={status_after!r}"
    )


# ---------------------------------------------------------------------------
# Invariant 2 — Status state machine: all four reachable from 'open'
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_state_machine_all_reachable(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    """Walk: open → triaged → acknowledged → closed → triaged.

    Each transition asserts 200 OK; the DB row reflects the final
    status. Audit log shows 4 annotation.triaged rows (3 forward + 1
    reverse).
    """
    suffix = uuid.uuid4().hex[:6]
    persona = harness.add_persona(
        f"sm-{suffix}", roles=["producer", "consumer", "admin"]
    )
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _materialise(harness, client, persona)
        await _seed_vocabulary(pg_container, persona.slug)
        cap_id = await _create_capability(
            harness, client, persona, name=f"cap-sm-{suffix}"
        )
        tenant_id = await _lookup_tenant_id(pg_container, persona.slug)

        harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            create_resp = await client.post(
                f"/v1/capabilities/{cap_id}/annotations",
                json={"body": "State machine test annotation.", "category": "suggestion"},
                headers=bearer_headers(tenant_slug=persona.slug),
            )
            assert create_resp.status_code == 201, create_resp.text
            annotation_id = uuid.UUID(create_resp.json()["annotation_id"])

            for target_status in ("triaged", "acknowledged", "closed"):
                resp = await client.patch(
                    f"/v1/annotations/{annotation_id}",
                    json={"status": target_status},
                    headers=bearer_headers(tenant_slug=persona.slug),
                )
                assert resp.status_code == 200, (
                    f"PATCH to {target_status!r} must return 200; "
                    f"got {resp.status_code}: {resp.text}"
                )
                assert resp.json()["status"] == target_status

            db_status = await _fetch_annotation_status(
                pg_container, annotation_id=annotation_id
            )
            assert db_status == "closed", f"DB after forward walk: got {db_status!r}"

            reverse_resp = await client.patch(
                f"/v1/annotations/{annotation_id}",
                json={"status": "triaged"},
                headers=bearer_headers(tenant_slug=persona.slug),
            )
            assert reverse_resp.status_code == 200, (
                f"closed → triaged must succeed; got {reverse_resp.status_code}: {reverse_resp.text}"
            )

    final_status = await _fetch_annotation_status(
        pg_container, annotation_id=annotation_id
    )
    assert final_status == "triaged", f"DB after reverse: got {final_status!r}"

    triage_count = await _count_audit_rows(
        pg_container,
        tenant_id=tenant_id,
        action="annotation.triaged",
        annotation_id=annotation_id,
    )
    assert triage_count == 4, (
        f"Expected 4 annotation.triaged audit rows (3 forward + 1 reverse); got {triage_count}"
    )


# ---------------------------------------------------------------------------
# Invariant 3 — assert_visible called exactly once per create_annotation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assert_visible_invoked_per_create(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    """Wrap VisibilityService.assert_visible with a counting shim and
    create two annotations; the counter must reach exactly 2."""
    suffix = uuid.uuid4().hex[:6]
    persona = harness.add_persona(
        f"vis-count-{suffix}", roles=["producer", "consumer"]
    )
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _materialise(harness, client, persona)
        await _seed_vocabulary(pg_container, persona.slug)
        cap_id = await _create_capability(
            harness, client, persona, name=f"cap-vis-count-{suffix}"
        )

        vis_svc = harness.app.state.visibility
        call_count = 0
        original = vis_svc.assert_visible

        async def _counting_assert_visible(ctx: Any, entity_id: Any) -> None:
            nonlocal call_count
            call_count += 1
            await original(ctx, entity_id)

        vis_svc.assert_visible = _counting_assert_visible
        try:
            harness.configure_fetcher_for(persona)
            with patch_validator_for_actor(persona):
                # Take the count after capability creation as the baseline —
                # the create_capability path also runs assert_visible.
                baseline = call_count
                resp1 = await client.post(
                    f"/v1/capabilities/{cap_id}/annotations",
                    json={"body": "Vis check 1.", "category": "feedback"},
                    headers=bearer_headers(tenant_slug=persona.slug),
                )
                assert resp1.status_code == 201, resp1.text
                assert call_count == baseline + 1, (
                    f"assert_visible must increment by 1 after first create; "
                    f"baseline={baseline} now={call_count}"
                )
                resp2 = await client.post(
                    f"/v1/capabilities/{cap_id}/annotations",
                    json={"body": "Vis check 2.", "category": "question"},
                    headers=bearer_headers(tenant_slug=persona.slug),
                )
                assert resp2.status_code == 201, resp2.text
                assert call_count == baseline + 2, (
                    f"assert_visible must increment by 2 after second create; "
                    f"baseline={baseline} now={call_count}"
                )
        finally:
            vis_svc.assert_visible = original
