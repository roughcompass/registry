"""PII scan integration tests for WorkspaceService create_entry and update_entry.

Pins the three-outcome dispatch (block / warn / advisory) for both
workspace_entry.body (body_md) and workspace_entry.references (references_jsonb).
Also covers skip-when-None and dual-field-warn paths.

All tests use AsyncMock DB and an injected mock PIIScanner — no Postgres required.
The advisory stub in the non-PII test module returns None; here we use full
PiiScanResponse objects to exercise real dispatch logic in the service.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from registry.service.workspace import WorkspaceEntryRef, WorkspaceService
from registry.types import FakeClock, PiiMatchResult, PiiScanResponse, TenantContext

_NOW = datetime.datetime(2026, 5, 12, 12, 0, 0, tzinfo=datetime.UTC)
_TENANT_A = uuid.uuid4()
_ACTOR_A = uuid.uuid4()
_WORKSPACE_ID = uuid.uuid4()
_ENTRY_ID = uuid.uuid4()


# ---------------------------------------------------------------------------
# PII scanner mock factories
# ---------------------------------------------------------------------------


def _pii_advisory() -> MagicMock:
    """Scanner that always returns a no-match advisory result."""
    scanner = MagicMock()
    scanner.scan = MagicMock(return_value=PiiScanResponse(matched_patterns=[], action_taken="advisory"))
    return scanner


def _pii_warn(field: str = "email", category: str = "CONTACT") -> MagicMock:
    """Scanner that always returns a warn result with one matched pattern."""
    scanner = MagicMock()
    scanner.scan = MagicMock(
        return_value=PiiScanResponse(
            matched_patterns=[PiiMatchResult(name=field, offset=0, length=10, category=category)],
            action_taken="warn",
        )
    )
    return scanner


def _pii_block(field: str = "email", category: str = "CONTACT") -> MagicMock:
    """Scanner that always returns a block result."""
    scanner = MagicMock()
    scanner.scan = MagicMock(
        return_value=PiiScanResponse(
            matched_patterns=[PiiMatchResult(name=field, offset=0, length=10, category=category)],
            action_taken="block",
        )
    )
    return scanner


# ---------------------------------------------------------------------------
# Session / factory helpers
# ---------------------------------------------------------------------------


def _make_workspace_row() -> MagicMock:
    row = MagicMock()
    row.workspace_id = _WORKSPACE_ID
    row.tenant_id = _TENANT_A
    row.name = "Test Workspace"
    row.description = None
    row.owner_kind = "actor"
    row.owner_actor_id = _ACTOR_A
    row.archived_at = None
    row.t_invalidated_at = None
    row.created_at = _NOW
    row.updated_at = _NOW
    row.created_by = _ACTOR_A
    return row


def _make_entry_row(
    *,
    body_md: str = "Some entry content",
    references_jsonb: dict[str, Any] | None = None,
) -> MagicMock:
    row = MagicMock()
    row.entry_id = _ENTRY_ID
    row.workspace_id = _WORKSPACE_ID
    row.tenant_id = _TENANT_A
    row.kind = "note"
    row.body_md = body_md
    row.references_jsonb = references_jsonb
    row.reference_ids = []
    row.expires_at = None
    row.t_invalidated_at = None
    row.created_at = _NOW
    row.updated_at = _NOW
    row.created_by = _ACTOR_A
    return row


def _make_actor_role_row(role_name: str) -> MagicMock:
    """Build a mock actor_roles row for _load_effective_roles."""
    row = MagicMock()
    row.name = role_name
    return row


def _make_session(
    *,
    is_regulated: bool = False,
    entry_row: MagicMock | None = None,
    actor_roles: list[str] | None = None,
) -> AsyncMock:
    """Session mock routing by SQL keyword fragments.

    Routes:
      SELECT ... FROM tenants          → regulated flag
      SELECT ... FROM workspaces       → workspace row (same-tenant owner)
      SELECT ... FROM actor_roles      → role-name rows for _load_effective_roles
      SELECT ... FROM workspace_entries → entry_row for update paths
      INSERT INTO workspace_entries    → no-op
      UPDATE workspace_entries         → no-op
    """
    _roles = actor_roles if actor_roles is not None else ["producer"]

    async def _execute(stmt: Any, params: dict | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        result = MagicMock()

        if "FROM tenants" in sql:
            tenant_row = MagicMock()
            tenant_row.is_regulated = is_regulated
            result.first = MagicMock(return_value=tenant_row)
            return result

        if "FROM workspaces" in sql:
            result.first = MagicMock(return_value=_make_workspace_row())
            return result

        if "FROM actor_roles" in sql:
            role_rows = [_make_actor_role_row(r) for r in _roles]
            result.fetchall = MagicMock(return_value=role_rows)
            result.__iter__ = MagicMock(return_value=iter(role_rows))
            return result

        if "INSERT INTO workspace_entries" in sql:
            result.first = MagicMock(return_value=None)
            return result

        if "UPDATE workspace_entries" in sql:
            result.first = MagicMock(return_value=None)
            return result

        if "FROM workspace_entries" in sql:
            result.first = MagicMock(return_value=entry_row)
            result.fetchall = MagicMock(return_value=[])
            return result

        result.first = MagicMock(return_value=None)
        result.fetchall = MagicMock(return_value=[])
        return result

    session = AsyncMock()
    session.execute = _execute
    return session


def _make_factory(session: AsyncMock) -> MagicMock:
    """Two-level async context manager factory the service expects."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)

    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    factory = MagicMock()
    factory.return_value = cm
    return factory


def _audit_writer() -> MagicMock:
    writer = MagicMock()
    writer.emit = AsyncMock(return_value=None)
    return writer


def _visibility() -> MagicMock:
    vis = MagicMock()
    vis.assert_visible = AsyncMock(return_value=None)
    return vis


def _ctx() -> TenantContext:
    return TenantContext(tenant_id=_TENANT_A, actor_id=_ACTOR_A, roles=["producer"])


def _make_service(
    *,
    pii_scanner: MagicMock,
    entry_row: MagicMock | None = None,
    audit_writer: MagicMock | None = None,
) -> WorkspaceService:
    session = _make_session(entry_row=entry_row)
    return WorkspaceService(
        session_factory=_make_factory(session),
        visibility_svc=_visibility(),
        pii_scanner=pii_scanner,
        audit_writer=audit_writer or _audit_writer(),
        clock=FakeClock(_NOW),
    )


# ---------------------------------------------------------------------------
# (1) create_entry body_md advisory → 201, no warnings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_body_advisory_returns_ref_no_warnings() -> None:
    """Advisory on body_md: entry stored, WorkspaceEntryRef returned, warnings is None."""
    svc = _make_service(pii_scanner=_pii_advisory())

    ref = await svc.create_entry(
        _ctx(),
        workspace_id=_WORKSPACE_ID,
        kind="note",
        body_md="Clean note content.",
        reference_ids=[],
    )

    assert isinstance(ref, WorkspaceEntryRef)
    assert ref.warnings is None


# ---------------------------------------------------------------------------
# (2) create_entry body_md warn → 201, warnings populated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_body_warn_returns_ref_with_warnings() -> None:
    """Warn on body_md: entry stored, warnings list has one entry with field='body_md'."""
    svc = _make_service(pii_scanner=_pii_warn())

    ref = await svc.create_entry(
        _ctx(),
        workspace_id=_WORKSPACE_ID,
        kind="note",
        body_md="Email user@example.com for details.",
        reference_ids=[],
    )

    assert isinstance(ref, WorkspaceEntryRef)
    assert ref.warnings is not None
    assert len(ref.warnings) == 1
    assert ref.warnings[0]["field"] == "body_md"
    assert isinstance(ref.warnings[0]["categories"], list)
    assert len(ref.warnings[0]["categories"]) > 0


# ---------------------------------------------------------------------------
# (3) create_entry body_md block → 422, categories in detail, no entry row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_body_block_raises_422_with_categories() -> None:
    """Block on body_md: 422 raised with structured detail including categories."""
    svc = _make_service(pii_scanner=_pii_block())

    with pytest.raises(HTTPException) as exc_info:
        await svc.create_entry(
            _ctx(),
            workspace_id=_WORKSPACE_ID,
            kind="note",
            body_md="SSN: 123-45-6789",
            reference_ids=[],
        )

    exc = exc_info.value
    assert exc.status_code == 422
    detail = exc.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "pii_detected"
    assert detail["field"] == "workspace_entry.body"
    assert isinstance(detail["categories"], list)
    assert len(detail["categories"]) > 0


@pytest.mark.asyncio
async def test_create_body_block_no_insert_issued() -> None:
    """Block on body_md: INSERT must not be issued — no entry row created."""
    writer = _audit_writer()
    # Track all SQL executed via a custom session that records statements.
    executed: list[str] = []

    async def _recording_execute(stmt: Any, params: dict | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        executed.append(sql)
        result = MagicMock()
        if "FROM tenants" in sql:
            tenant_row = MagicMock()
            tenant_row.is_regulated = False
            result.first = MagicMock(return_value=tenant_row)
            return result
        if "FROM workspaces" in sql:
            result.first = MagicMock(return_value=_make_workspace_row())
            return result
        if "FROM actor_roles" in sql:
            role_rows = [_make_actor_role_row("producer")]
            result.fetchall = MagicMock(return_value=role_rows)
            result.__iter__ = MagicMock(return_value=iter(role_rows))
            return result
        result.first = MagicMock(return_value=None)
        result.fetchall = MagicMock(return_value=[])
        return result

    session = AsyncMock()
    session.execute = _recording_execute
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock()
    factory.return_value = cm

    svc = WorkspaceService(
        session_factory=factory,
        visibility_svc=_visibility(),
        pii_scanner=_pii_block(),
        audit_writer=writer,
        clock=FakeClock(_NOW),
    )

    with pytest.raises(HTTPException):
        await svc.create_entry(
            _ctx(),
            workspace_id=_WORKSPACE_ID,
            kind="note",
            body_md="SSN: 123-45-6789",
            reference_ids=[],
        )

    insert_calls = [s for s in executed if "INSERT INTO workspace_entries" in s]
    assert insert_calls == [], "No INSERT should be issued on a block"
    writer.emit.assert_not_called()


# ---------------------------------------------------------------------------
# (4) create_entry references_jsonb advisory → 201, no warnings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_refs_advisory_returns_ref_no_warnings() -> None:
    """Advisory on references_jsonb: entry stored, warnings is None."""
    svc = _make_service(pii_scanner=_pii_advisory())

    ref = await svc.create_entry(
        _ctx(),
        workspace_id=_WORKSPACE_ID,
        kind="saved_query",
        body_md="Query body",
        reference_ids=[],
        references_jsonb={"source": "system-a", "ids": ["x1", "x2"]},
    )

    assert isinstance(ref, WorkspaceEntryRef)
    assert ref.warnings is None


# ---------------------------------------------------------------------------
# (5) create_entry references_jsonb warn → 201, warnings include field="references_jsonb"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_refs_warn_warnings_include_references_jsonb_field() -> None:
    """Warn on references_jsonb: warnings list has entry with field='references_jsonb'."""
    svc = _make_service(pii_scanner=_pii_warn())

    ref = await svc.create_entry(
        _ctx(),
        workspace_id=_WORKSPACE_ID,
        kind="saved_query",
        body_md="Query body",
        reference_ids=[],
        references_jsonb={"email": "user@example.com"},
    )

    assert isinstance(ref, WorkspaceEntryRef)
    assert ref.warnings is not None
    fields_in_warnings = [w["field"] for w in ref.warnings]
    assert "references_jsonb" in fields_in_warnings


# ---------------------------------------------------------------------------
# Scan skipped when references_jsonb=None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_refs_scan_skipped_when_none() -> None:
    """Scanner is not called for references field when references_jsonb is None."""
    scanner = _pii_advisory()
    svc = _make_service(pii_scanner=scanner)

    await svc.create_entry(
        _ctx(),
        workspace_id=_WORKSPACE_ID,
        kind="note",
        body_md="Note without refs",
        reference_ids=[],
        references_jsonb=None,
    )

    # Only the body scan should have fired; references scan must not.
    scan_field_types = [c.kwargs.get("field_type", "") for c in scanner.scan.call_args_list]
    assert not any("workspace_entry.references" in ft for ft in scan_field_types)
    # Body scan still fires exactly once.
    body_calls = [ft for ft in scan_field_types if "workspace_entry.body" in ft]
    assert len(body_calls) == 1


# ---------------------------------------------------------------------------
# (6) update_entry body_md block → 422, UPDATE not applied
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_body_block_raises_422_no_update() -> None:
    """Block on update body_md: 422 raised, UPDATE must not be issued."""
    entry_row = _make_entry_row()
    executed: list[str] = []

    async def _recording_execute(stmt: Any, params: dict | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        executed.append(sql)
        result = MagicMock()
        if "FROM tenants" in sql:
            tenant_row = MagicMock()
            tenant_row.is_regulated = False
            result.first = MagicMock(return_value=tenant_row)
            return result
        if "FROM workspaces" in sql:
            result.first = MagicMock(return_value=_make_workspace_row())
            return result
        if "FROM actor_roles" in sql:
            role_rows = [_make_actor_role_row("producer")]
            result.fetchall = MagicMock(return_value=role_rows)
            result.__iter__ = MagicMock(return_value=iter(role_rows))
            return result
        if "FROM workspace_entries" in sql:
            result.first = MagicMock(return_value=entry_row)
            return result
        result.first = MagicMock(return_value=None)
        result.fetchall = MagicMock(return_value=[])
        return result

    session = AsyncMock()
    session.execute = _recording_execute
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock()
    factory.return_value = cm

    writer = _audit_writer()
    svc = WorkspaceService(
        session_factory=factory,
        visibility_svc=_visibility(),
        pii_scanner=_pii_block(),
        audit_writer=writer,
        clock=FakeClock(_NOW),
    )

    with pytest.raises(HTTPException) as exc_info:
        await svc.update_entry(
            _ctx(),
            entry_id=_ENTRY_ID,
            body_md="Updated body with SSN: 123-45-6789",
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["code"] == "pii_detected"
    assert exc_info.value.detail["field"] == "workspace_entry.body"

    update_calls = [s for s in executed if "UPDATE workspace_entries" in s]
    assert update_calls == [], "No UPDATE should be issued on a block"
    writer.emit.assert_not_called()


# ---------------------------------------------------------------------------
# (7) update_entry body_md=None → scanner not called for body
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_body_none_scanner_not_called_for_body() -> None:
    """When body_md is None, the PII scanner is not called for the body field."""
    scanner = _pii_advisory()
    entry_row = _make_entry_row()
    svc = _make_service(pii_scanner=scanner, entry_row=entry_row)

    # Only pass reference_ids; body_md omitted (defaults to None).
    ref = await svc.update_entry(
        _ctx(),
        entry_id=_ENTRY_ID,
        reference_ids=[],
    )

    assert isinstance(ref, WorkspaceEntryRef)

    scan_field_types = [c.kwargs.get("field_type", "") for c in scanner.scan.call_args_list]
    body_calls = [ft for ft in scan_field_types if "workspace_entry.body" in ft]
    assert body_calls == [], "Scanner must not be called for body when body_md is None"


# ---------------------------------------------------------------------------
# (8) Both fields hit warn → warnings list has two entries, one per field
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_both_fields_warn_two_warning_entries() -> None:
    """When both body_md and references_jsonb warn, warnings list has exactly two entries."""
    scanner = MagicMock()
    # Return warn for every scan call regardless of field.
    scanner.scan = MagicMock(
        return_value=PiiScanResponse(
            matched_patterns=[PiiMatchResult(name="email", offset=0, length=10, category="CONTACT")],
            action_taken="warn",
        )
    )
    svc = _make_service(pii_scanner=scanner)

    ref = await svc.create_entry(
        _ctx(),
        workspace_id=_WORKSPACE_ID,
        kind="saved_query",
        body_md="Email user@example.com in body.",
        reference_ids=[],
        references_jsonb={"email": "other@example.com"},
    )

    assert isinstance(ref, WorkspaceEntryRef)
    assert ref.warnings is not None
    assert len(ref.warnings) == 2, f"Expected 2 warnings, got {len(ref.warnings)}: {ref.warnings}"

    warning_fields = {w["field"] for w in ref.warnings}
    assert "body_md" in warning_fields
    assert "references_jsonb" in warning_fields
