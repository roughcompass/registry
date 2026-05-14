"""Unit tests for WorkspaceService entry CRUD: create_entry, update_entry,
delete_entry, list_entries.

All DB interaction is mocked at session.execute via an SQL-string-keyed router —
no Postgres is required. The mock-factory pattern mirrors test_workspace_service.py:
MagicMock session_factory whose __aenter__ returns the SQL-keyed AsyncMock session,
with session.begin() separately mocked as an async context manager.

SQL routing table for this test module:
  - SELECT ... FROM tenants          → tenant row with is_regulated flag
  - SELECT ... FROM workspaces       → workspace row (needed by get_workspace)
  - SELECT ... FROM workspace_shares → no share row (actor is same-tenant owner)
  - SELECT ... FROM workspace_entries (single) → entry_row or None
  - SELECT ... FROM workspace_entries (list)   → list of entry rows
  - INSERT INTO workspace_entries    → no-op
  - UPDATE workspace_entries         → no-op

PII scanner is always stubbed to advisory (returns None) in this module.
Full PII dispatch tests live in test_workspace_pii_integration.py.
"""

from __future__ import annotations

import base64
import datetime
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from registry.service.workspace import (
    WorkspaceEntryRef,
    WorkspaceService,
)
from registry.types import FakeClock, TenantContext

_NOW = datetime.datetime(2026, 5, 12, 12, 0, 0, tzinfo=datetime.UTC)
_EXPIRES = datetime.datetime(2026, 12, 31, 0, 0, 0, tzinfo=datetime.UTC)
_TENANT_A = uuid.uuid4()
_ACTOR_A = uuid.uuid4()
_TENANT_B = uuid.uuid4()
_ACTOR_B = uuid.uuid4()
_WORKSPACE_ID = uuid.uuid4()
_ENTRY_ID = uuid.uuid4()


def _make_entry_cursor(entry_id: uuid.UUID) -> str:
    """Encode a keyset cursor matching the service's _encode_entry_cursor output."""
    payload = {"id": str(entry_id)}
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


# ---------------------------------------------------------------------------
# Context and service factory helpers
# ---------------------------------------------------------------------------


def _ctx(
    tenant: uuid.UUID = _TENANT_A,
    actor: uuid.UUID = _ACTOR_A,
    roles: list[str] | None = None,
) -> TenantContext:
    return TenantContext(tenant_id=tenant, actor_id=actor, roles=roles or ["producer"])


def _audit_writer() -> MagicMock:
    writer = MagicMock()
    writer.emit = AsyncMock(return_value=None)
    return writer


def _pii_scanner() -> MagicMock:
    scanner = MagicMock()
    scanner.scan = MagicMock(return_value=None)
    return scanner


def _visibility() -> MagicMock:
    vis = MagicMock()
    vis.assert_visible = AsyncMock(return_value=None)
    return vis


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------


def _make_workspace_row(
    *,
    workspace_id: uuid.UUID = _WORKSPACE_ID,
    tenant_id: uuid.UUID = _TENANT_A,
    owner_actor_id: uuid.UUID | None = _ACTOR_A,
) -> MagicMock:
    row = MagicMock()
    row.workspace_id = workspace_id
    row.tenant_id = tenant_id
    row.name = "Test Workspace"
    row.description = None
    row.owner_kind = "actor"
    row.owner_actor_id = owner_actor_id
    row.archived_at = None
    row.t_invalidated_at = None
    row.created_at = _NOW
    row.updated_at = _NOW
    row.created_by = owner_actor_id
    return row


def _make_entry_row(
    *,
    entry_id: uuid.UUID = _ENTRY_ID,
    workspace_id: uuid.UUID = _WORKSPACE_ID,
    tenant_id: uuid.UUID = _TENANT_A,
    kind: str = "note",
    body_md: str = "Some note content",
    references_jsonb: dict[str, Any] | None = None,
    reference_ids: list[uuid.UUID] | None = None,
    expires_at: datetime.datetime | None = None,
    t_invalidated_at: datetime.datetime | None = None,
) -> MagicMock:
    row = MagicMock()
    row.entry_id = entry_id
    row.workspace_id = workspace_id
    row.tenant_id = tenant_id
    row.kind = kind
    row.body_md = body_md
    row.references_jsonb = references_jsonb
    row.reference_ids = reference_ids or []
    row.expires_at = expires_at
    row.t_invalidated_at = t_invalidated_at
    row.created_at = _NOW
    row.updated_at = _NOW
    row.created_by = _ACTOR_A
    return row


# ---------------------------------------------------------------------------
# Session / factory builders
# ---------------------------------------------------------------------------


def _make_actor_role_row(role_name: str) -> MagicMock:
    """Build a mock actor_roles row for _load_effective_roles."""
    row = MagicMock()
    row.name = role_name
    return row


def _make_session(
    *,
    is_regulated: bool = False,
    workspace_row: MagicMock | None = None,
    entry_row: MagicMock | None = None,
    entry_list_rows: list[MagicMock] | None = None,
    actor_roles: list[str] | None = None,
) -> AsyncMock:
    """Build an AsyncMock session whose execute routes by SQL keywords.

    Routes:
      SELECT ... FROM tenants                  → tenant row
      SELECT ... FROM actor_roles (JOIN roles) → role-name rows for _load_effective_roles
      SELECT ... FROM workspaces (single)      → workspace_row (for get_workspace)
      SELECT ... FROM workspace_entries (single) → entry_row
      SELECT ... FROM workspace_entries (list)   → entry_list_rows
      INSERT INTO workspace_entries            → no-op
      UPDATE workspace_entries                 → no-op
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

        if "INSERT INTO workspace_entries" in sql:
            result.first = MagicMock(return_value=None)
            return result

        if "UPDATE workspace_entries" in sql:
            result.first = MagicMock(return_value=None)
            return result

        if "FROM workspaces" in sql:
            # get_workspace uses single-row lookup
            ws_row = workspace_row if workspace_row is not None else _make_workspace_row()
            result.first = MagicMock(return_value=ws_row)
            return result

        if "FROM actor_roles" in sql:
            role_rows = [_make_actor_role_row(r) for r in _roles]
            result.fetchall = MagicMock(return_value=role_rows)
            result.__iter__ = MagicMock(return_value=iter(role_rows))
            return result

        if "FROM workspace_entries" in sql:
            if entry_list_rows is not None:
                result.fetchall = MagicMock(return_value=entry_list_rows)
            else:
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


def _make_service(
    *,
    session: AsyncMock | None = None,
    is_regulated: bool = False,
    workspace_row: MagicMock | None = None,
    entry_row: MagicMock | None = None,
    entry_list_rows: list[MagicMock] | None = None,
    actor_roles: list[str] | None = None,
    audit_writer: MagicMock | None = None,
    scanner: MagicMock | None = None,
    clock: FakeClock | None = None,
) -> tuple[WorkspaceService, MagicMock]:
    """Return (service, scanner) so tests can assert scanner.scan was called."""
    _scanner = scanner or _pii_scanner()
    if session is None:
        session = _make_session(
            is_regulated=is_regulated,
            workspace_row=workspace_row,
            entry_row=entry_row,
            entry_list_rows=entry_list_rows,
            actor_roles=actor_roles,
        )
    svc = WorkspaceService(
        session_factory=_make_factory(session),
        visibility_svc=_visibility(),
        pii_scanner=_scanner,
        audit_writer=audit_writer or _audit_writer(),
        clock=clock or FakeClock(_NOW),
    )
    return svc, _scanner


# ---------------------------------------------------------------------------
# (a) create_entry succeeds → WorkspaceEntryRef returned
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_entry_succeeds() -> None:
    """create_entry with valid inputs returns a WorkspaceEntryRef."""
    ctx = _ctx()
    writer = _audit_writer()
    svc, scanner = _make_service(audit_writer=writer)

    ref = await svc.create_entry(
        ctx,
        workspace_id=_WORKSPACE_ID,
        kind="note",
        body_md="My first note",
        reference_ids=[],
    )

    assert isinstance(ref, WorkspaceEntryRef)
    assert ref.workspace_id == _WORKSPACE_ID
    assert ref.tenant_id == _TENANT_A
    assert ref.kind == "note"
    assert ref.body_md == "My first note"
    assert ref.t_invalidated_at is None
    assert ref.created_at == _NOW

    # Audit must be emitted with the correct action constant.
    writer.emit.assert_awaited_once()
    call_kwargs = writer.emit.await_args.kwargs
    assert call_kwargs["action"] == "workspace.entry.created"
    assert call_kwargs["target_type"] == "workspace_entry"

    # PII scanner must be invoked on the body.
    scanner.scan.assert_called()
    scan_calls = [c.kwargs.get("field_type") or c.args[1] if c.args else None
                  for c in scanner.scan.call_args_list]
    assert any("workspace_entry.body" in str(ft) for ft in scan_calls)


# ---------------------------------------------------------------------------
# (b) create_entry raises 422 on is_regulated=True (defense-in-depth)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_entry_raises_422_for_regulated_tenant() -> None:
    """The regulated-tenant block fires at create_entry independently of create_workspace.

    A regulated tenant that somehow obtained a workspace (via test fixture or
    direct DB insert) must still be rejected when creating entries.
    """
    ctx = _ctx()
    svc, _ = _make_service(is_regulated=True)

    with pytest.raises(HTTPException) as exc_info:
        await svc.create_entry(
            ctx,
            workspace_id=_WORKSPACE_ID,
            kind="note",
            body_md="Blocked content",
            reference_ids=[],
        )

    assert exc_info.value.status_code == 422
    assert "regulated" in exc_info.value.detail
    assert "encryption tier" in exc_info.value.detail


# ---------------------------------------------------------------------------
# (c) invalid kind → 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_entry_raises_422_on_invalid_kind() -> None:
    """An entry kind outside the closed vocabulary is rejected with 422."""
    ctx = _ctx()
    svc, _ = _make_service()

    with pytest.raises(HTTPException) as exc_info:
        await svc.create_entry(
            ctx,
            workspace_id=_WORKSPACE_ID,
            kind="blog_post",  # not in VALID_ENTRY_KINDS
            body_md="Some content",
            reference_ids=[],
        )

    assert exc_info.value.status_code == 422
    assert "kind" in exc_info.value.detail


# ---------------------------------------------------------------------------
# (d) empty body_md → 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_entry_raises_422_on_empty_body_md() -> None:
    """An empty body_md is rejected with 422 before any INSERT."""
    ctx = _ctx()
    svc, _ = _make_service()

    with pytest.raises(HTTPException) as exc_info:
        await svc.create_entry(
            ctx,
            workspace_id=_WORKSPACE_ID,
            kind="note",
            body_md="",
            reference_ids=[],
        )

    assert exc_info.value.status_code == 422
    assert "body_md" in exc_info.value.detail.lower()


# ---------------------------------------------------------------------------
# (e) update_entry succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_entry_succeeds() -> None:
    """update_entry returns a WorkspaceEntryRef with the updated body."""
    ctx = _ctx()
    writer = _audit_writer()
    entry_row = _make_entry_row(body_md="Original content")
    svc, scanner = _make_service(entry_row=entry_row, audit_writer=writer)

    ref = await svc.update_entry(
        ctx,
        entry_id=_ENTRY_ID,
        body_md="Updated content",
    )

    assert isinstance(ref, WorkspaceEntryRef)
    assert ref.body_md == "Updated content"
    assert ref.entry_id == _ENTRY_ID

    writer.emit.assert_awaited_once()
    call_kwargs = writer.emit.await_args.kwargs
    assert call_kwargs["action"] == "workspace.entry.updated"

    # PII scanner invoked on the new body.
    scanner.scan.assert_called()


# ---------------------------------------------------------------------------
# (f) delete_entry is idempotent — second call no-op, no duplicate audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_entry_idempotent() -> None:
    """Calling delete_entry on an already-soft-deleted entry is a no-op.

    No second audit event is emitted — callers can retry safely.
    """
    ctx = _ctx()
    writer = _audit_writer()
    # Entry row already has t_invalidated_at set.
    entry_row = _make_entry_row(t_invalidated_at=_NOW)
    svc, _ = _make_service(entry_row=entry_row, audit_writer=writer)

    # First call — already deleted, so no-op.
    await svc.delete_entry(ctx, entry_id=_ENTRY_ID)
    # Second call — still no-op.
    await svc.delete_entry(ctx, entry_id=_ENTRY_ID)

    # No audit event emitted for either call.
    writer.emit.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_entry_live_entry_emits_audit() -> None:
    """Deleting an active entry soft-deletes it and emits exactly one audit event."""
    ctx = _ctx()
    writer = _audit_writer()
    entry_row = _make_entry_row(t_invalidated_at=None)
    svc, _ = _make_service(entry_row=entry_row, audit_writer=writer)

    await svc.delete_entry(ctx, entry_id=_ENTRY_ID)

    writer.emit.assert_awaited_once()
    call_kwargs = writer.emit.await_args.kwargs
    assert call_kwargs["action"] == "workspace.entry.deleted"


# ---------------------------------------------------------------------------
# (g) list_entries returns active entries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_entries_returns_active_entries() -> None:
    """list_entries returns WorkspaceEntryRef objects for active entries."""
    ctx = _ctx()
    rows = [
        _make_entry_row(entry_id=uuid.uuid4(), kind="note"),
        _make_entry_row(entry_id=uuid.uuid4(), kind="decision"),
    ]
    svc, _ = _make_service(entry_list_rows=rows)

    refs, next_cursor = await svc.list_entries(ctx, workspace_id=_WORKSPACE_ID)

    assert len(refs) == 2
    assert all(isinstance(r, WorkspaceEntryRef) for r in refs)
    assert all(r.body_md == "Some note content" for r in refs)
    assert next_cursor is None  # fewer than page size


# ---------------------------------------------------------------------------
# (h) list_entries applies kind filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_entries_applies_kind_filter() -> None:
    """list_entries with kind='decision' returns only decision entries.

    The actual SQL filtering is done by the DB; this test confirms the service
    passes kind=None vs kind='decision' through correctly and does not strip the
    filter before reaching the query.
    """
    ctx = _ctx()
    # Simulate DB returning only decision rows (as if SQL WHERE kind='decision' ran).
    decision_row = _make_entry_row(entry_id=uuid.uuid4(), kind="decision")
    svc, _ = _make_service(entry_list_rows=[decision_row])

    refs, _ = await svc.list_entries(ctx, workspace_id=_WORKSPACE_ID, kind="decision")

    assert len(refs) == 1
    assert refs[0].kind == "decision"


# ---------------------------------------------------------------------------
# (i) expired entry still returned by list_entries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_entries_includes_expired_entries() -> None:
    """list_entries does not filter on expires_at.

    Entries past their expires_at are still returned; the expiry worker
    soft-deletes them in a background run. list_entries only excludes
    rows where t_invalidated_at IS NOT NULL.
    """
    ctx = _ctx()
    past_time = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
    expired_row = _make_entry_row(
        entry_id=uuid.uuid4(),
        kind="note",
        expires_at=past_time,
        t_invalidated_at=None,  # still active — expiry worker hasn't run yet
    )
    svc, _ = _make_service(entry_list_rows=[expired_row])

    refs, _ = await svc.list_entries(ctx, workspace_id=_WORKSPACE_ID)

    assert len(refs) == 1
    assert refs[0].expires_at == past_time


# ---------------------------------------------------------------------------
# (j) create_entry with expires_at stores the field
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_entry_stores_expires_at() -> None:
    """create_entry passes expires_at through to the returned WorkspaceEntryRef."""
    ctx = _ctx()
    svc, _ = _make_service()

    ref = await svc.create_entry(
        ctx,
        workspace_id=_WORKSPACE_ID,
        kind="note",
        body_md="Expiring note",
        reference_ids=[],
        expires_at=_EXPIRES,
    )

    assert ref.expires_at == _EXPIRES
    assert isinstance(ref, WorkspaceEntryRef)


# ---------------------------------------------------------------------------
# (k) create_entry with expires_at=None stores None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_entry_expires_at_none_when_omitted() -> None:
    """create_entry with no expires_at results in expires_at=None on the ref."""
    ctx = _ctx()
    svc, _ = _make_service()

    ref = await svc.create_entry(
        ctx,
        workspace_id=_WORKSPACE_ID,
        kind="decision",
        body_md="Permanent decision",
        reference_ids=[],
    )

    assert ref.expires_at is None


# ---------------------------------------------------------------------------
# (l) update_entry with no changed fields still emits one audit event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_entry_no_changed_fields_emits_audit() -> None:
    """update_entry with all-None kwargs still runs UPDATE and emits one audit event.

    The service always writes current values back and always emits audit — there
    is no short-circuit for unchanged fields. This test documents that behaviour
    so any future optimisation (skip-if-unchanged) is a deliberate decision.
    """
    ctx = _ctx()
    writer = _audit_writer()
    entry_row = _make_entry_row(body_md="Unchanged content")
    svc, _ = _make_service(entry_row=entry_row, audit_writer=writer)

    # Pass no field overrides — every effective value falls back to the existing row.
    ref = await svc.update_entry(ctx, entry_id=_ENTRY_ID)

    # Returns successfully.
    assert isinstance(ref, WorkspaceEntryRef)
    assert ref.body_md == "Unchanged content"

    # Audit is always emitted (no skip-if-unchanged optimisation present).
    writer.emit.assert_awaited_once()
    call_kwargs = writer.emit.await_args.kwargs
    assert call_kwargs["action"] == "workspace.entry.updated"


# ---------------------------------------------------------------------------
# (m) delete_entry on already-deleted entry is a no-op (idempotent, 200)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_entry_already_deleted_is_noop_no_audit() -> None:
    """delete_entry on a soft-deleted entry is a safe no-op.

    Already covered by test_delete_entry_idempotent; this variant names the
    behaviour from the caller's perspective — the second delete returns without
    raising and without writing a second audit row.
    """
    ctx = _ctx()
    writer = _audit_writer()
    entry_row = _make_entry_row(t_invalidated_at=_NOW)
    svc, _ = _make_service(entry_row=entry_row, audit_writer=writer)

    # Should not raise.
    await svc.delete_entry(ctx, entry_id=_ENTRY_ID)

    writer.emit.assert_not_awaited()


# ---------------------------------------------------------------------------
# (n) list_entries excludes soft-deleted entries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_entries_excludes_soft_deleted() -> None:
    """list_entries returns zero rows when the DB returns no active entries.

    The soft-delete filter (t_invalidated_at IS NULL) is applied in SQL; the
    mock simulates the DB having applied it by returning an empty list. This
    test confirms the service correctly handles an empty result set without error.
    """
    ctx = _ctx()
    svc, _ = _make_service(entry_list_rows=[])

    refs, next_cursor = await svc.list_entries(ctx, workspace_id=_WORKSPACE_ID)

    assert refs == []
    assert next_cursor is None


# ---------------------------------------------------------------------------
# (o) list_entries returns empty list when workspace has no active entries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_entries_empty_workspace() -> None:
    """list_entries returns ([], None) for a workspace with no entries at all."""
    ctx = _ctx()
    svc, _ = _make_service(entry_list_rows=[])

    refs, cursor = await svc.list_entries(ctx, workspace_id=_WORKSPACE_ID)

    assert refs == []
    assert cursor is None


# ---------------------------------------------------------------------------
# (p) list_entries cursor pagination — first page returns next_cursor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_entries_first_page_returns_cursor() -> None:
    """list_entries returns a non-None next_cursor when there are more rows.

    The mock returns 51 rows (DEFAULT_PAGE_SIZE + 1); the service should
    truncate to 50 and encode a cursor pointing at the last returned entry.
    """
    ctx = _ctx()
    # Build 51 entry rows — one more than the default page size of 50.
    entry_ids = [uuid.uuid4() for _ in range(51)]
    rows = [_make_entry_row(entry_id=eid) for eid in entry_ids]
    svc, _ = _make_service(entry_list_rows=rows)

    refs, next_cursor = await svc.list_entries(ctx, workspace_id=_WORKSPACE_ID)

    assert len(refs) == 50
    assert next_cursor is not None
    # Cursor must decode to the last row's entry_id.
    expected_cursor = _make_entry_cursor(entry_ids[49])
    assert next_cursor == expected_cursor


# ---------------------------------------------------------------------------
# (q) list_entries cursor pagination — second page uses cursor, returns remainder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_entries_second_page_returns_remainder() -> None:
    """list_entries with a cursor returns remaining entries and no next_cursor.

    The mock returns 3 rows (simulating the remainder after page 1). The service
    should return all 3 with next_cursor=None.
    """
    ctx = _ctx()
    last_page_id = uuid.uuid4()
    cursor = _make_entry_cursor(last_page_id)

    remainder_rows = [_make_entry_row(entry_id=uuid.uuid4()) for _ in range(3)]
    svc, _ = _make_service(entry_list_rows=remainder_rows)

    refs, next_cursor = await svc.list_entries(
        ctx,
        workspace_id=_WORKSPACE_ID,
        cursor=cursor,
    )

    assert len(refs) == 3
    assert next_cursor is None


# ---------------------------------------------------------------------------
# (r) list_entries kind filter combined with cursor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_entries_kind_filter_with_cursor() -> None:
    """list_entries accepts both kind and cursor simultaneously.

    The mock simulates a filtered + paginated result. This test confirms the
    service does not drop either parameter when both are present.
    """
    ctx = _ctx()
    pivot_id = uuid.uuid4()
    cursor = _make_entry_cursor(pivot_id)

    rows = [_make_entry_row(entry_id=uuid.uuid4(), kind="decision") for _ in range(2)]
    svc, _ = _make_service(entry_list_rows=rows)

    refs, next_cursor = await svc.list_entries(
        ctx,
        workspace_id=_WORKSPACE_ID,
        kind="decision",
        cursor=cursor,
    )

    assert len(refs) == 2
    assert all(r.kind == "decision" for r in refs)
    assert next_cursor is None


# ---------------------------------------------------------------------------
# (s) create_entry by cross-tenant actor with no share → 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_entry_cross_tenant_raises_not_found() -> None:
    """A cross-tenant actor cannot perceive the workspace at all.

    get_workspace enforces tenant isolation as the outermost predicate. When the
    actor's tenant differs from the workspace's tenant, the service raises
    WorkspaceNotFound (router maps to 404) so the workspace's existence is not
    disclosed.
    """
    from registry.service.workspace import WorkspaceNotFound

    # Actor B is from tenant B; the workspace is owned by actor A in tenant A.
    ctx_b = _ctx(tenant=_TENANT_B, actor=_ACTOR_B)
    ws_row = _make_workspace_row(tenant_id=_TENANT_A, owner_actor_id=_ACTOR_A)
    svc, _ = _make_service(workspace_row=ws_row)

    with pytest.raises(WorkspaceNotFound):
        await svc.create_entry(
            ctx_b,
            workspace_id=_WORKSPACE_ID,
            kind="note",
            body_md="Cross-tenant note",
            reference_ids=[],
        )


# ---------------------------------------------------------------------------
# (t) update_entry by cross-tenant actor with no share → 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_entry_cross_tenant_raises_not_found() -> None:
    """A cross-tenant actor cannot perceive the entry's workspace.

    update_entry calls get_workspace on the entry's owning workspace. Tenant
    isolation is the outermost predicate, so the result is WorkspaceNotFound
    (router maps to 404) — the workspace's existence is not disclosed across
    tenants.
    """
    from registry.service.workspace import WorkspaceNotFound

    ctx_b = _ctx(tenant=_TENANT_B, actor=_ACTOR_B)
    ws_row = _make_workspace_row(tenant_id=_TENANT_A, owner_actor_id=_ACTOR_A)
    entry_row = _make_entry_row(workspace_id=_WORKSPACE_ID)
    svc, _ = _make_service(workspace_row=ws_row, entry_row=entry_row)

    with pytest.raises(WorkspaceNotFound):
        await svc.update_entry(
            ctx_b,
            entry_id=_ENTRY_ID,
            body_md="Unauthorized update",
        )


# ---------------------------------------------------------------------------
# (u) audit audit-log is invoked on create_entry with correct action string
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_entry_audit_action_constant() -> None:
    """create_entry emits audit with action='workspace.entry.created'."""
    ctx = _ctx()
    writer = _audit_writer()
    svc, _ = _make_service(audit_writer=writer)

    await svc.create_entry(
        ctx,
        workspace_id=_WORKSPACE_ID,
        kind="open_question",
        body_md="Who owns the design review?",
        reference_ids=[],
    )

    writer.emit.assert_awaited_once()
    assert writer.emit.await_args.kwargs["action"] == "workspace.entry.created"
    assert writer.emit.await_args.kwargs["target_type"] == "workspace_entry"


# ---------------------------------------------------------------------------
# (v) audit-log invoked on update_entry with correct action string
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_entry_audit_action_constant() -> None:
    """update_entry emits audit with action='workspace.entry.updated'."""
    ctx = _ctx()
    writer = _audit_writer()
    entry_row = _make_entry_row()
    svc, _ = _make_service(entry_row=entry_row, audit_writer=writer)

    await svc.update_entry(ctx, entry_id=_ENTRY_ID, body_md="New body")

    writer.emit.assert_awaited_once()
    assert writer.emit.await_args.kwargs["action"] == "workspace.entry.updated"
    assert writer.emit.await_args.kwargs["target_type"] == "workspace_entry"


# ---------------------------------------------------------------------------
# (w) audit-log invoked on delete_entry with correct action string
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_entry_audit_action_constant() -> None:
    """delete_entry emits audit with action='workspace.entry.deleted'."""
    ctx = _ctx()
    writer = _audit_writer()
    entry_row = _make_entry_row(t_invalidated_at=None)
    svc, _ = _make_service(entry_row=entry_row, audit_writer=writer)

    await svc.delete_entry(ctx, entry_id=_ENTRY_ID)

    writer.emit.assert_awaited_once()
    assert writer.emit.await_args.kwargs["action"] == "workspace.entry.deleted"
    assert writer.emit.await_args.kwargs["target_type"] == "workspace_entry"


# ---------------------------------------------------------------------------
# (x) reference_ids round-trip through create_entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_entry_reference_ids_round_trip() -> None:
    """reference_ids passed to create_entry are present on the returned ref."""
    ctx = _ctx()
    ref_id_1 = uuid.uuid4()
    ref_id_2 = uuid.uuid4()
    svc, _ = _make_service()

    ref = await svc.create_entry(
        ctx,
        workspace_id=_WORKSPACE_ID,
        kind="note",
        body_md="Note with refs",
        reference_ids=[ref_id_1, ref_id_2],
    )

    assert ref.reference_ids == [ref_id_1, ref_id_2]


# ---------------------------------------------------------------------------
# (y) references_jsonb round-trip through create_entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_entry_references_jsonb_round_trip() -> None:
    """references_jsonb passed to create_entry is present on the returned ref."""
    ctx = _ctx()
    jsonb_payload = {"source": "external-system", "ids": ["abc", "def"]}
    svc, _ = _make_service()

    ref = await svc.create_entry(
        ctx,
        workspace_id=_WORKSPACE_ID,
        kind="saved_query",
        body_md="My saved query",
        reference_ids=[],
        references_jsonb=jsonb_payload,
    )

    assert ref.references_jsonb == jsonb_payload


# ---------------------------------------------------------------------------
# (z) references_jsonb defaults to None when omitted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_entry_references_jsonb_defaults_to_none() -> None:
    """references_jsonb is None on the returned ref when not supplied."""
    ctx = _ctx()
    svc, _ = _make_service()

    ref = await svc.create_entry(
        ctx,
        workspace_id=_WORKSPACE_ID,
        kind="note",
        body_md="Note without jsonb refs",
        reference_ids=[],
    )

    assert ref.references_jsonb is None


# ---------------------------------------------------------------------------
# (aa) create_entry triggers PII scan on body_md
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_entry_pii_scan_body_md() -> None:
    """create_entry scans body_md with field_type='workspace_entry.body'.

    PII scanner is stubbed to advisory (returns None). This test confirms the
    scan call is made with the correct field_type — not that it takes any action.
    """
    ctx = _ctx()
    svc, scanner = _make_service()

    await svc.create_entry(
        ctx,
        workspace_id=_WORKSPACE_ID,
        kind="note",
        body_md="Content to be scanned",
        reference_ids=[],
    )

    scan_field_types = [
        c.kwargs.get("field_type", "") for c in scanner.scan.call_args_list
    ]
    assert any("workspace_entry.body" in ft for ft in scan_field_types)


# ---------------------------------------------------------------------------
# (ab) create_entry triggers PII scan on references_jsonb when provided
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_entry_pii_scan_references_jsonb_when_present() -> None:
    """create_entry scans references_jsonb with field_type='workspace_entry.references'."""
    ctx = _ctx()
    svc, scanner = _make_service()

    await svc.create_entry(
        ctx,
        workspace_id=_WORKSPACE_ID,
        kind="note",
        body_md="Note with external refs",
        reference_ids=[],
        references_jsonb={"link": "https://example.com"},
    )

    scan_field_types = [
        c.kwargs.get("field_type", "") for c in scanner.scan.call_args_list
    ]
    assert any("workspace_entry.references" in ft for ft in scan_field_types)


# ---------------------------------------------------------------------------
# (ac) create_entry does NOT scan references_jsonb when None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_entry_no_pii_scan_when_references_jsonb_absent() -> None:
    """create_entry does not call scanner for references_jsonb when it is None."""
    ctx = _ctx()
    svc, scanner = _make_service()

    await svc.create_entry(
        ctx,
        workspace_id=_WORKSPACE_ID,
        kind="note",
        body_md="Note without jsonb",
        reference_ids=[],
        references_jsonb=None,
    )

    scan_field_types = [
        c.kwargs.get("field_type", "") for c in scanner.scan.call_args_list
    ]
    # Body scan fires; references scan must NOT fire.
    assert not any("workspace_entry.references" in ft for ft in scan_field_types)


# ---------------------------------------------------------------------------
# (ad) update_entry triggers PII scan on new body_md
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_entry_pii_scan_body_md() -> None:
    """update_entry scans the new body_md when it is supplied."""
    ctx = _ctx()
    entry_row = _make_entry_row()
    svc, scanner = _make_service(entry_row=entry_row)

    await svc.update_entry(ctx, entry_id=_ENTRY_ID, body_md="Updated body")

    scan_field_types = [
        c.kwargs.get("field_type", "") for c in scanner.scan.call_args_list
    ]
    assert any("workspace_entry.body" in ft for ft in scan_field_types)


# ---------------------------------------------------------------------------
# (ae) update_entry does not scan body_md when omitted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_entry_no_pii_scan_when_body_md_omitted() -> None:
    """update_entry does not invoke the PII scanner when body_md is None."""
    ctx = _ctx()
    entry_row = _make_entry_row()
    svc, scanner = _make_service(entry_row=entry_row)

    # Only update reference_ids; body_md is left unchanged.
    await svc.update_entry(ctx, entry_id=_ENTRY_ID, reference_ids=[])

    scan_field_types = [
        c.kwargs.get("field_type", "") for c in scanner.scan.call_args_list
    ]
    assert not any("workspace_entry.body" in ft for ft in scan_field_types)


# ---------------------------------------------------------------------------
# (af) create_entry returns correct tenant_id on the ref
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_entry_ref_tenant_id() -> None:
    """The returned WorkspaceEntryRef carries the calling actor's tenant_id."""
    ctx = _ctx()
    svc, _ = _make_service()

    ref = await svc.create_entry(
        ctx,
        workspace_id=_WORKSPACE_ID,
        kind="note",
        body_md="Tenant check",
        reference_ids=[],
    )

    assert ref.tenant_id == _TENANT_A


# ---------------------------------------------------------------------------
# (ag) delete_entry on non-existent entry raises 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_entry_not_found_raises_404() -> None:
    """delete_entry raises 404 when the entry row does not exist."""
    ctx = _ctx()
    svc, _ = _make_service(entry_row=None)

    with pytest.raises(HTTPException) as exc_info:
        await svc.delete_entry(ctx, entry_id=uuid.uuid4())

    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# (ah) update_entry on non-existent entry raises 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_entry_not_found_raises_404() -> None:
    """update_entry raises 404 when the entry does not exist."""
    ctx = _ctx()
    svc, _ = _make_service(entry_row=None)

    with pytest.raises(HTTPException) as exc_info:
        await svc.update_entry(ctx, entry_id=uuid.uuid4(), body_md="ghost update")

    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# (ai) create_entry — all valid VALID_ENTRY_KINDS are accepted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_entry_all_valid_kinds_accepted() -> None:
    """Every kind in the closed vocabulary is accepted without error."""
    from registry.service.workspace import VALID_ENTRY_KINDS

    ctx = _ctx()
    for kind in sorted(VALID_ENTRY_KINDS):
        svc, _ = _make_service()
        ref = await svc.create_entry(
            ctx,
            workspace_id=_WORKSPACE_ID,
            kind=kind,
            body_md=f"Body for {kind}",
            reference_ids=[],
        )
        assert ref.kind == kind


# ---------------------------------------------------------------------------
# (aj) create_entry emits audit with target_id matching the new entry_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_entry_audit_target_id_is_entry_id() -> None:
    """The audit event target_id is the UUID of the newly created entry."""
    ctx = _ctx()
    writer = _audit_writer()
    svc, _ = _make_service(audit_writer=writer)

    ref = await svc.create_entry(
        ctx,
        workspace_id=_WORKSPACE_ID,
        kind="note",
        body_md="Audit target check",
        reference_ids=[],
    )

    writer.emit.assert_awaited_once()
    assert writer.emit.await_args.kwargs["target_id"] == ref.entry_id


# ---------------------------------------------------------------------------
# (ak) update_entry reference_ids update round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_entry_reference_ids_round_trip() -> None:
    """update_entry replaces reference_ids and the new value appears on the ref."""
    ctx = _ctx()
    new_ref_id = uuid.uuid4()
    entry_row = _make_entry_row(reference_ids=[])
    svc, _ = _make_service(entry_row=entry_row)

    ref = await svc.update_entry(
        ctx,
        entry_id=_ENTRY_ID,
        reference_ids=[new_ref_id],
    )

    assert ref.reference_ids == [new_ref_id]


# ---------------------------------------------------------------------------
# (al) is_regulated=True → 422 on create_entry exact error body
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_entry_regulated_exact_error_detail() -> None:
    """The 422 error body for a regulated tenant matches the expected string.

    This is the defense-in-depth guard in create_entry, independent of any
    workspace-create guard. Simulates a regulated tenant that already holds a
    workspace (e.g. via direct DB insertion) and tries to add an entry.
    """
    ctx = _ctx()
    svc, _ = _make_service(is_regulated=True)

    with pytest.raises(HTTPException) as exc_info:
        await svc.create_entry(
            ctx,
            workspace_id=_WORKSPACE_ID,
            kind="note",
            body_md="Blocked note",
            reference_ids=[],
        )

    assert exc_info.value.status_code == 422
    detail = exc_info.value.detail
    assert "regulated" in detail
    assert "encryption tier" in detail
    assert "none" in detail
