"""Unit tests for WorkspaceService: create_workspace, get_workspace, list_workspaces.

All DB interaction is mocked at session.execute via an SQL-string-keyed router —
no Postgres is required. VisibilityService, PIIScanner, and AuditWriter are each
replaced with lightweight AsyncMock / MagicMock fixtures.

Mock-factory pattern: MagicMock whose __aenter__ returns the SQL-string-keyed
AsyncMock session. session.begin() is separately mocked as an async context manager
because the service uses compound async with:
  async with self._session_factory() as session, session.begin(): ...
Omitting the session.begin() mock causes AttributeError: __aenter__.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from registry.service.workspace import (
    WorkspaceNotFound,
    WorkspaceOperationDenied,
    WorkspaceRef,
    WorkspaceService,
)
from registry.types import FakeClock, TenantContext

_NOW = datetime.datetime(2026, 5, 12, 12, 0, 0, tzinfo=datetime.UTC)
_TENANT_A = uuid.uuid4()   # workspace owning tenant
_TENANT_B = uuid.uuid4()   # second tenant (for cross-tenant isolation tests)
_ACTOR_A = uuid.uuid4()    # owner actor
_ACTOR_B = uuid.uuid4()    # second actor
_WORKSPACE_ID = uuid.uuid4()


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


def _pii_clean() -> MagicMock:
    scanner = MagicMock()
    scanner.scan = MagicMock()
    return scanner


def _visibility() -> MagicMock:
    vis = MagicMock()
    vis.assert_visible = AsyncMock(return_value=None)
    return vis


def _make_workspace_row(
    *,
    workspace_id: uuid.UUID = _WORKSPACE_ID,
    tenant_id: uuid.UUID = _TENANT_A,
    owner_kind: str = "actor",
    owner_actor_id: uuid.UUID | None = _ACTOR_A,
    archived_at: datetime.datetime | None = None,
    t_invalidated_at: datetime.datetime | None = None,
) -> MagicMock:
    """Build a mock workspace row returned by the DB."""
    row = MagicMock()
    row.workspace_id = workspace_id
    row.tenant_id = tenant_id
    row.name = "My Workspace"
    row.description = None
    row.owner_kind = owner_kind
    row.owner_actor_id = owner_actor_id
    row.archived_at = archived_at
    row.t_invalidated_at = t_invalidated_at
    row.created_at = _NOW
    row.updated_at = _NOW
    row.created_by = owner_actor_id
    return row


def _make_actor_role_row(role_name: str) -> MagicMock:
    """Build a mock actor_roles row for _load_effective_roles."""
    row = MagicMock()
    row.name = role_name
    return row


def _make_session(
    *,
    is_regulated: bool = False,
    workspace_row: MagicMock | None = None,
    actor_roles: list[str] | None = None,
    list_rows: list[MagicMock] | None = None,
) -> AsyncMock:
    """Build an AsyncMock session whose execute routes by SQL keywords.

    Routes:
    - SELECT ... FROM tenants         → tenant row with is_regulated
    - INSERT INTO workspaces          → no-op
    - UPDATE workspaces               → no-op
    - SELECT ... FROM actor_roles     → role name rows for _load_effective_roles
    - SELECT ... FROM workspaces (single row) → workspace_row or None
    - SELECT ... FROM workspaces w (list)     → list_rows
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

        if "INSERT INTO workspaces" in sql:
            result.first = MagicMock(return_value=None)
            return result

        if "UPDATE workspaces" in sql:
            result.first = MagicMock(return_value=None)
            return result

        if "FROM workspaces w" in sql:
            # list_workspaces / search_workspaces query (checked before actor_roles to
            # avoid misrouting queries that embed EXISTS(SELECT FROM actor_roles))
            rows = list_rows if list_rows is not None else []
            result.fetchall = MagicMock(return_value=rows)
            return result

        if "FROM workspaces" in sql and "workspace_id = :workspace_id" in sql:
            # get_workspace single-row lookup
            result.first = MagicMock(return_value=workspace_row)
            return result

        if "FROM actor_roles" in sql:
            # _load_effective_roles query
            role_rows = [_make_actor_role_row(r) for r in _roles]
            result.fetchall = MagicMock(return_value=role_rows)
            result.__iter__ = MagicMock(return_value=iter(role_rows))
            return result

        result.first = MagicMock(return_value=None)
        result.fetchall = MagicMock(return_value=[])
        return result

    session = AsyncMock()
    session.execute = _execute
    return session


def _make_factory(session: AsyncMock) -> MagicMock:
    """Wrap a mock session in the two-level factory mock the service expects.

    The service calls: async with self._session_factory() as session, session.begin():
    Both async context manager levels must be wired.
    """
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
    actor_roles: list[str] | None = None,
    list_rows: list[MagicMock] | None = None,
    audit_writer: MagicMock | None = None,
    clock: FakeClock | None = None,
) -> WorkspaceService:
    if session is None:
        session = _make_session(
            is_regulated=is_regulated,
            workspace_row=workspace_row,
            actor_roles=actor_roles,
            list_rows=list_rows,
        )
    return WorkspaceService(
        session_factory=_make_factory(session),
        visibility_svc=_visibility(),
        pii_scanner=_pii_clean(),
        audit_writer=audit_writer or _audit_writer(),
        clock=clock or FakeClock(_NOW),
    )


# ---------------------------------------------------------------------------
# (a) create_workspace — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_workspace_succeeds_actor_owner() -> None:
    """create_workspace returns a WorkspaceRef with correct fields for actor owner."""
    ctx = _ctx()
    writer = _audit_writer()
    svc = _make_service(audit_writer=writer)

    ref = await svc.create_workspace(ctx, name="My WS", owner_kind="actor")

    assert isinstance(ref, WorkspaceRef)
    assert ref.name == "My WS"
    assert ref.owner_kind == "actor"
    assert ref.owner_actor_id == ctx.actor_id
    assert ref.tenant_id == ctx.tenant_id
    assert ref.created_at == _NOW
    assert ref.t_invalidated_at is None
    assert ref.archived_at is None
    writer.emit.assert_awaited_once()
    call_kwargs = writer.emit.await_args.kwargs
    assert call_kwargs["action"] == "workspace.created"
    assert call_kwargs["target_type"] == "workspace"


@pytest.mark.asyncio
async def test_create_workspace_succeeds_tenant_owner() -> None:
    """create_workspace with owner_kind='tenant' sets owner_actor_id=None."""
    ctx = _ctx()
    svc = _make_service(actor_roles=["admin"])

    ref = await svc.create_workspace(ctx, name="Team WS", owner_kind="tenant")

    assert ref.owner_kind == "tenant"
    assert ref.owner_actor_id is None


# ---------------------------------------------------------------------------
# (b) create_workspace — regulated tenant raises 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_workspace_raises_422_for_regulated_tenant() -> None:
    """Regulated tenants cannot create workspaces while encryption_tier='none'."""
    ctx = _ctx()
    svc = _make_service(is_regulated=True)

    with pytest.raises(HTTPException) as exc_info:
        await svc.create_workspace(ctx, name="Blocked", owner_kind="actor")

    assert exc_info.value.status_code == 422
    assert "regulated" in exc_info.value.detail
    assert "encryption tier" in exc_info.value.detail


# ---------------------------------------------------------------------------
# (c) create_workspace — invalid owner_kind raises 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_workspace_raises_422_on_invalid_owner_kind() -> None:
    """Invalid owner_kind values are rejected with a 422 before any INSERT."""
    ctx = _ctx()
    svc = _make_service()

    with pytest.raises(HTTPException) as exc_info:
        await svc.create_workspace(ctx, name="WS", owner_kind="group")

    assert exc_info.value.status_code == 422
    assert "owner_kind" in exc_info.value.detail


# ---------------------------------------------------------------------------
# (d) create_workspace — role gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_workspace_producer_denied_for_tenant_kind() -> None:
    """Producer may not create tenant-owned workspaces; admin role is required."""
    ctx = _ctx()
    svc = _make_service(actor_roles=["producer"])

    with pytest.raises(WorkspaceOperationDenied):
        await svc.create_workspace(ctx, name="Team WS", owner_kind="tenant")


@pytest.mark.asyncio
async def test_create_workspace_admin_denied_for_actor_kind() -> None:
    """Admin without producer may not create actor-owned workspaces."""
    ctx = _ctx()
    svc = _make_service(actor_roles=["admin"])

    with pytest.raises(WorkspaceOperationDenied):
        await svc.create_workspace(ctx, name="Personal WS", owner_kind="actor")


@pytest.mark.asyncio
async def test_create_workspace_no_role_denied() -> None:
    """Actors with no roles are denied before owner_kind is evaluated."""
    ctx = _ctx()
    svc = _make_service(actor_roles=[])

    with pytest.raises(WorkspaceOperationDenied):
        await svc.create_workspace(ctx, name="WS", owner_kind="actor")


@pytest.mark.asyncio
async def test_create_workspace_admin_and_producer_may_create_both_kinds() -> None:
    """Multi-role actors (admin + producer) may create workspaces of either kind."""
    ctx = _ctx()
    svc = _make_service(actor_roles=["admin", "producer"])

    ref_actor = await svc.create_workspace(ctx, name="Actor WS", owner_kind="actor")
    ref_tenant = await svc.create_workspace(ctx, name="Tenant WS", owner_kind="tenant")

    assert ref_actor.owner_kind == "actor"
    assert ref_tenant.owner_kind == "tenant"


# ---------------------------------------------------------------------------
# (d) get_workspace — returns workspace for owning actor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_workspace_returns_workspace_for_owner() -> None:
    """get_workspace returns WorkspaceRef when the caller is the owning actor."""
    ctx = _ctx(tenant=_TENANT_A, actor=_ACTOR_A)
    ws_row = _make_workspace_row(owner_actor_id=_ACTOR_A)
    svc = _make_service(workspace_row=ws_row)

    ref = await svc.get_workspace(ctx, _WORKSPACE_ID)

    assert isinstance(ref, WorkspaceRef)
    assert ref.workspace_id == _WORKSPACE_ID
    assert ref.owner_actor_id == _ACTOR_A


# ---------------------------------------------------------------------------
# (e) get_workspace — 404 for actor with no roles (not perceivable)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_workspace_raises_404_for_no_roles() -> None:
    """get_workspace raises when the actor has no roles in their tenant."""
    ctx = _ctx(tenant=_TENANT_A, actor=_ACTOR_B)
    ws_row = _make_workspace_row(
        tenant_id=_TENANT_A,
        owner_kind="tenant",
        owner_actor_id=None,
    )
    # actor_roles=[] → _load_effective_roles returns frozenset() → not perceivable
    svc = _make_service(workspace_row=ws_row, actor_roles=[])
    with pytest.raises(WorkspaceNotFound):
        await svc.get_workspace(ctx, _WORKSPACE_ID)


# ---------------------------------------------------------------------------
# (g) get_workspace — 404 for soft-deleted workspace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_workspace_raises_not_found_for_missing() -> None:
    """get_workspace raises WorkspaceNotFound when workspace_row is None (no such workspace)."""
    ctx = _ctx()
    # workspace_row=None means no row returned from the DB
    svc = _make_service(workspace_row=None)

    with pytest.raises(WorkspaceNotFound):
        await svc.get_workspace(ctx, _WORKSPACE_ID)


# ---------------------------------------------------------------------------
# (h) get_workspace — same-tenant member can access tenant-owned workspace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_workspace_same_tenant_member_can_access_tenant_workspace() -> None:
    """A same-tenant actor with any role can access a tenant-owned workspace."""
    other_actor = uuid.uuid4()
    ctx = _ctx(tenant=_TENANT_A, actor=other_actor)
    ws_row = _make_workspace_row(
        tenant_id=_TENANT_A,
        owner_kind="tenant",
        owner_actor_id=None,
    )
    svc = _make_service(workspace_row=ws_row, actor_roles=["consumer"])

    ref = await svc.get_workspace(ctx, _WORKSPACE_ID)

    assert ref.workspace_id == _WORKSPACE_ID


# ---------------------------------------------------------------------------
# (i) list_workspaces — returns owned workspaces
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_workspaces_returns_owned() -> None:
    """list_workspaces returns workspaces visible to the calling actor."""
    ctx = _ctx()
    ws_row = _make_workspace_row()
    svc = _make_service(list_rows=[ws_row])

    refs, next_cursor = await svc.list_workspaces(ctx)

    assert len(refs) == 1
    assert refs[0].workspace_id == _WORKSPACE_ID
    assert next_cursor is None


# ---------------------------------------------------------------------------
# (j) list_workspaces — excludes archived when include_archived=False
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_workspaces_excludes_archived_by_default() -> None:
    """When include_archived=False (default), archived_at IS NULL is enforced at the SQL layer.

    This test verifies that the SQL issued by list_workspaces includes the
    archived_at IS NULL clause when include_archived=False.
    """
    ctx = _ctx()
    sql_issued: list[str] = []

    async def _capturing_execute(stmt: Any, params: dict | None = None) -> MagicMock:
        sql_issued.append(" ".join(str(stmt).split()))
        result = MagicMock()
        result.fetchall = MagicMock(return_value=[])
        return result

    session = AsyncMock()
    session.execute = _capturing_execute
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=cm)

    svc = WorkspaceService(
        session_factory=factory,
        visibility_svc=_visibility(),
        pii_scanner=_pii_clean(),
        audit_writer=_audit_writer(),
        clock=FakeClock(_NOW),
    )

    await svc.list_workspaces(ctx, include_archived=False)

    assert any("archived_at IS NULL" in sql for sql in sql_issued), (
        "Expected 'archived_at IS NULL' in the issued SQL when include_archived=False"
    )


# ---------------------------------------------------------------------------
# (k) list_workspaces — includes archived when include_archived=True
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_workspaces_includes_archived_when_requested() -> None:
    """When include_archived=True, archived workspaces appear in results."""
    ctx = _ctx()
    archived_row = _make_workspace_row(archived_at=_NOW)
    sql_issued: list[str] = []

    async def _capturing_execute(stmt: Any, params: dict | None = None) -> MagicMock:
        sql_issued.append(" ".join(str(stmt).split()))
        result = MagicMock()
        result.fetchall = MagicMock(return_value=[archived_row])
        return result

    session = AsyncMock()
    session.execute = _capturing_execute
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=cm)

    svc = WorkspaceService(
        session_factory=factory,
        visibility_svc=_visibility(),
        pii_scanner=_pii_clean(),
        audit_writer=_audit_writer(),
        clock=FakeClock(_NOW),
    )

    refs, _ = await svc.list_workspaces(ctx, include_archived=True)

    # Archived clause must NOT appear in the SQL when include_archived=True
    assert not any("archived_at IS NULL" in sql for sql in sql_issued), (
        "archived_at IS NULL clause must be absent when include_archived=True"
    )
    assert len(refs) == 1
    assert refs[0].archived_at == _NOW


# ---------------------------------------------------------------------------
# (a) update_workspace — update name succeeds, audit written
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_workspace_name_succeeds_and_audits() -> None:
    """update_workspace returns an updated WorkspaceRef and writes one audit row."""
    ctx = _ctx(tenant=_TENANT_A, actor=_ACTOR_A)
    ws_row = _make_workspace_row(owner_actor_id=_ACTOR_A)
    writer = _audit_writer()
    svc = _make_service(workspace_row=ws_row, audit_writer=writer)

    ref = await svc.update_workspace(ctx, _WORKSPACE_ID, name="Renamed WS")

    assert isinstance(ref, WorkspaceRef)
    assert ref.name == "Renamed WS"
    assert ref.workspace_id == _WORKSPACE_ID
    writer.emit.assert_awaited()
    # The last emit call must be for the update action
    last_call = writer.emit.await_args
    assert last_call.kwargs["action"] == "workspace.updated"
    assert last_call.kwargs["target_type"] == "workspace"


# ---------------------------------------------------------------------------
# (b) update_workspace — non-owner raises 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_workspace_by_consumer_raises_denied() -> None:
    """update_workspace raises WorkspaceOperationDenied when the caller is a consumer."""
    # Consumer can perceive actor-owned workspace they own, but cannot write metadata.
    ctx = _ctx(tenant=_TENANT_A, actor=_ACTOR_A, roles=["consumer"])
    ws_row = _make_workspace_row(tenant_id=_TENANT_A, owner_actor_id=_ACTOR_A)
    svc = _make_service(workspace_row=ws_row, actor_roles=["consumer"])

    with pytest.raises(WorkspaceOperationDenied):
        await svc.update_workspace(ctx, _WORKSPACE_ID, name="Hacked")


# ---------------------------------------------------------------------------
# (c) update_workspace — archived_at set archives workspace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_workspace_sets_archived_at() -> None:
    """Passing archived_at archives the workspace; the returned ref carries the value."""
    ctx = _ctx(tenant=_TENANT_A, actor=_ACTOR_A)
    ws_row = _make_workspace_row(owner_actor_id=_ACTOR_A, archived_at=None)
    svc = _make_service(workspace_row=ws_row)

    archive_time = datetime.datetime(2026, 5, 12, 15, 0, 0, tzinfo=datetime.UTC)
    ref = await svc.update_workspace(ctx, _WORKSPACE_ID, archived_at=archive_time)

    assert ref.archived_at == archive_time


# ---------------------------------------------------------------------------
# (d) update_workspace — archived_at=None un-archives
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_workspace_unarchives_when_archived_at_is_none() -> None:
    """Passing archived_at=None clears the archived state in the returned ref."""
    ctx = _ctx(tenant=_TENANT_A, actor=_ACTOR_A)
    ws_row = _make_workspace_row(owner_actor_id=_ACTOR_A, archived_at=_NOW)
    svc = _make_service(workspace_row=ws_row)

    ref = await svc.update_workspace(ctx, _WORKSPACE_ID, archived_at=None)

    assert ref.archived_at is None


# ---------------------------------------------------------------------------
# (e) delete_workspace — owner soft-deletes successfully
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_workspace_by_owner_succeeds() -> None:
    """delete_workspace completes without error for the owning actor and audits once."""
    ctx = _ctx(tenant=_TENANT_A, actor=_ACTOR_A)
    ws_row = _make_workspace_row(owner_actor_id=_ACTOR_A, t_invalidated_at=None)
    writer = _audit_writer()
    svc = _make_service(workspace_row=ws_row, audit_writer=writer)

    await svc.delete_workspace(ctx, _WORKSPACE_ID)

    writer.emit.assert_awaited_once()
    call_kwargs = writer.emit.await_args.kwargs
    assert call_kwargs["action"] == "workspace.deleted"
    assert call_kwargs["target_id"] == _WORKSPACE_ID


# ---------------------------------------------------------------------------
# (f) delete_workspace — non-owner raises 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_workspace_by_non_owner_raises_denied() -> None:
    """delete_workspace raises WorkspaceOperationDenied when the caller is not the owner."""
    # ACTOR_B — not the owner of the actor-owned workspace (owner is ACTOR_A).
    ctx = _ctx(tenant=_TENANT_A, actor=_ACTOR_B, roles=["producer"])
    ws_row = _make_workspace_row(
        tenant_id=_TENANT_A,
        owner_kind="actor",
        owner_actor_id=_ACTOR_A,
        t_invalidated_at=None,
    )
    svc = _make_service(workspace_row=ws_row, actor_roles=["producer"])

    with pytest.raises(WorkspaceOperationDenied):
        await svc.delete_workspace(ctx, _WORKSPACE_ID)


# ---------------------------------------------------------------------------
# (g) delete_workspace — second call is a no-op (idempotent)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_workspace_second_call_is_noop() -> None:
    """delete_workspace is idempotent: a second call emits no audit row and raises no error."""
    ctx = _ctx(tenant=_TENANT_A, actor=_ACTOR_A)
    # Simulate workspace that is already soft-deleted
    ws_row = _make_workspace_row(owner_actor_id=_ACTOR_A, t_invalidated_at=_NOW)
    writer = _audit_writer()
    svc = _make_service(workspace_row=ws_row, audit_writer=writer)

    # Should not raise
    await svc.delete_workspace(ctx, _WORKSPACE_ID)

    # No audit must have been emitted on the no-op path
    writer.emit.assert_not_awaited()


# ---------------------------------------------------------------------------
# (h) delete_workspace — non-existent workspace raises 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_workspace_raises_not_found_for_nonexistent() -> None:
    """delete_workspace raises WorkspaceNotFound when no workspace row exists."""
    ctx = _ctx()
    # workspace_row=None → the raw SELECT returns nothing.
    svc = _make_service(workspace_row=None)

    with pytest.raises(WorkspaceNotFound):
        await svc.delete_workspace(ctx, _WORKSPACE_ID)


# ---------------------------------------------------------------------------
# Cross-cutting invariant: audit log for every mutation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_workspace_audit_target_id_matches_workspace_id() -> None:
    """audit_writer.emit target_id must equal the created workspace_id."""
    ctx = _ctx()
    writer = _audit_writer()
    svc = _make_service(audit_writer=writer)

    ref = await svc.create_workspace(ctx, name="WS", owner_kind="actor")

    call_kwargs = writer.emit.await_args.kwargs
    assert call_kwargs["target_id"] == ref.workspace_id


@pytest.mark.asyncio
async def test_create_workspace_audit_after_contains_required_keys() -> None:
    """audit after= dict for create must contain workspace_id, tenant_id, owner_kind, name."""
    ctx = _ctx()
    writer = _audit_writer()
    svc = _make_service(audit_writer=writer)

    await svc.create_workspace(ctx, name="WS Audit Keys", owner_kind="actor")

    call_kwargs = writer.emit.await_args.kwargs
    after = call_kwargs["after"]
    assert "workspace_id" in after
    assert "tenant_id" in after
    assert "owner_kind" in after
    assert "name" in after
    assert after["name"] == "WS Audit Keys"
    assert after["owner_kind"] == "actor"


@pytest.mark.asyncio
async def test_update_workspace_audit_target_id_matches_workspace_id() -> None:
    """audit_writer.emit target_id must equal the updated workspace_id."""
    ctx = _ctx(tenant=_TENANT_A, actor=_ACTOR_A)
    ws_row = _make_workspace_row(owner_actor_id=_ACTOR_A)
    writer = _audit_writer()
    svc = _make_service(workspace_row=ws_row, audit_writer=writer)

    await svc.update_workspace(ctx, _WORKSPACE_ID, name="New Name")

    last_call = writer.emit.await_args
    assert last_call.kwargs["target_id"] == _WORKSPACE_ID


@pytest.mark.asyncio
async def test_update_workspace_audit_after_contains_required_keys() -> None:
    """audit after= dict for update must contain workspace_id and name."""
    ctx = _ctx(tenant=_TENANT_A, actor=_ACTOR_A)
    ws_row = _make_workspace_row(owner_actor_id=_ACTOR_A)
    writer = _audit_writer()
    svc = _make_service(workspace_row=ws_row, audit_writer=writer)

    await svc.update_workspace(ctx, _WORKSPACE_ID, name="Auditable")

    last_call = writer.emit.await_args
    after = last_call.kwargs["after"]
    assert "workspace_id" in after
    assert "name" in after
    assert after["name"] == "Auditable"


@pytest.mark.asyncio
async def test_delete_workspace_audit_after_contains_required_keys() -> None:
    """audit after= dict for delete must contain workspace_id and t_invalidated_at."""
    ctx = _ctx(tenant=_TENANT_A, actor=_ACTOR_A)
    ws_row = _make_workspace_row(owner_actor_id=_ACTOR_A, t_invalidated_at=None)
    writer = _audit_writer()
    svc = _make_service(workspace_row=ws_row, audit_writer=writer)

    await svc.delete_workspace(ctx, _WORKSPACE_ID)

    call_kwargs = writer.emit.await_args.kwargs
    after = call_kwargs["after"]
    assert "workspace_id" in after
    assert "t_invalidated_at" in after


@pytest.mark.asyncio
async def test_audit_emitted_once_per_create() -> None:
    """create_workspace emits exactly one audit event per call."""
    ctx = _ctx()
    writer = _audit_writer()
    svc = _make_service(audit_writer=writer)

    await svc.create_workspace(ctx, name="W1", owner_kind="actor")

    assert writer.emit.await_count == 1


@pytest.mark.asyncio
async def test_audit_emitted_once_per_update() -> None:
    """update_workspace emits exactly one audit event per call."""
    ctx = _ctx(tenant=_TENANT_A, actor=_ACTOR_A)
    ws_row = _make_workspace_row(owner_actor_id=_ACTOR_A)
    writer = _audit_writer()
    svc = _make_service(workspace_row=ws_row, audit_writer=writer)

    await svc.update_workspace(ctx, _WORKSPACE_ID, name="Updated")

    assert writer.emit.await_count == 1


@pytest.mark.asyncio
async def test_audit_emitted_once_per_delete() -> None:
    """delete_workspace emits exactly one audit event per call."""
    ctx = _ctx(tenant=_TENANT_A, actor=_ACTOR_A)
    ws_row = _make_workspace_row(owner_actor_id=_ACTOR_A, t_invalidated_at=None)
    writer = _audit_writer()
    svc = _make_service(workspace_row=ws_row, audit_writer=writer)

    await svc.delete_workspace(ctx, _WORKSPACE_ID)

    assert writer.emit.await_count == 1


# ---------------------------------------------------------------------------
# update on soft-deleted workspace → 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_workspace_raises_not_found_for_missing() -> None:
    """update_workspace raises WorkspaceNotFound when the workspace does not exist."""
    ctx = _ctx(tenant=_TENANT_A, actor=_ACTOR_A)
    # workspace_row=None simulates a workspace row that does not exist.
    svc = _make_service(workspace_row=None)

    with pytest.raises(WorkspaceNotFound):
        await svc.update_workspace(ctx, _WORKSPACE_ID, name="Should fail")


# ---------------------------------------------------------------------------
# list_workspaces — cursor pagination boundaries
# ---------------------------------------------------------------------------


def _make_ws_rows(n: int) -> list[MagicMock]:
    """Build n distinct workspace rows with sequential workspace_ids."""
    rows = []
    for _ in range(n):
        wid = uuid.uuid4()
        rows.append(_make_workspace_row(workspace_id=wid))
    return rows


@pytest.mark.asyncio
async def test_list_workspaces_cursor_returned_when_full_page() -> None:
    """Exactly DEFAULT_PAGE_SIZE + 1 rows from DB → next_cursor is returned."""
    from registry.service.workspace import _DEFAULT_PAGE_SIZE

    ctx = _ctx()
    # Service fetches limit+1 rows; if it gets that many, has_next=True
    rows = _make_ws_rows(_DEFAULT_PAGE_SIZE + 1)
    svc = _make_service(list_rows=rows)

    refs, next_cursor = await svc.list_workspaces(ctx)

    assert len(refs) == _DEFAULT_PAGE_SIZE
    assert next_cursor is not None


@pytest.mark.asyncio
async def test_list_workspaces_no_cursor_when_below_page_size() -> None:
    """page_size - 1 rows from DB → next_cursor is None."""
    from registry.service.workspace import _DEFAULT_PAGE_SIZE

    ctx = _ctx()
    rows = _make_ws_rows(_DEFAULT_PAGE_SIZE - 1)
    svc = _make_service(list_rows=rows)

    refs, next_cursor = await svc.list_workspaces(ctx)

    assert len(refs) == _DEFAULT_PAGE_SIZE - 1
    assert next_cursor is None


@pytest.mark.asyncio
async def test_list_workspaces_no_cursor_when_exactly_page_size() -> None:
    """Exactly page_size rows from DB → no cursor (the +1 sentinel was not returned)."""
    from registry.service.workspace import _DEFAULT_PAGE_SIZE

    ctx = _ctx()
    rows = _make_ws_rows(_DEFAULT_PAGE_SIZE)
    svc = _make_service(list_rows=rows)

    refs, next_cursor = await svc.list_workspaces(ctx)

    assert len(refs) == _DEFAULT_PAGE_SIZE
    assert next_cursor is None


@pytest.mark.asyncio
async def test_list_workspaces_cursor_is_base64_string() -> None:
    """next_cursor is a base64-encoded string when returned."""
    import base64
    import json

    from registry.service.workspace import _DEFAULT_PAGE_SIZE

    ctx = _ctx()
    rows = _make_ws_rows(_DEFAULT_PAGE_SIZE + 1)
    svc = _make_service(list_rows=rows)

    _, next_cursor = await svc.list_workspaces(ctx)

    assert next_cursor is not None
    # Must be decodable to a JSON object with an "id" key.
    payload = json.loads(base64.urlsafe_b64decode(next_cursor.encode()).decode())
    assert "id" in payload
    uuid.UUID(payload["id"])  # must be a valid UUID


# ---------------------------------------------------------------------------
# create_workspace — owner_kind='tenant' sets owner_actor_id=None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_workspace_tenant_owner_actor_id_is_none() -> None:
    """create_workspace with owner_kind='tenant' returns owner_actor_id=None."""
    ctx = _ctx()
    svc = _make_service(actor_roles=["admin"])

    ref = await svc.create_workspace(ctx, name="Team WS", owner_kind="tenant")

    assert ref.owner_kind == "tenant"
    assert ref.owner_actor_id is None


@pytest.mark.asyncio
async def test_create_workspace_actor_owner_actor_id_is_set() -> None:
    """create_workspace with owner_kind='actor' sets owner_actor_id to ctx.actor_id."""
    ctx = _ctx()
    svc = _make_service()

    ref = await svc.create_workspace(ctx, name="Personal WS", owner_kind="actor")

    assert ref.owner_kind == "actor"
    assert ref.owner_actor_id == ctx.actor_id


# ---------------------------------------------------------------------------
# get_workspace — ref field correctness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_workspace_ref_fields_match_row() -> None:
    """get_workspace returns a WorkspaceRef whose fields exactly match the DB row."""
    ctx = _ctx(tenant=_TENANT_A, actor=_ACTOR_A)
    ws_row = _make_workspace_row(
        workspace_id=_WORKSPACE_ID,
        tenant_id=_TENANT_A,
        owner_kind="actor",
        owner_actor_id=_ACTOR_A,
        archived_at=None,
    )
    svc = _make_service(workspace_row=ws_row)

    ref = await svc.get_workspace(ctx, _WORKSPACE_ID)

    assert ref.workspace_id == _WORKSPACE_ID
    assert ref.tenant_id == _TENANT_A
    assert ref.owner_kind == "actor"
    assert ref.owner_actor_id == _ACTOR_A
    assert ref.archived_at is None
    assert ref.t_invalidated_at is None


@pytest.mark.asyncio
async def test_get_workspace_ref_name_description_from_row() -> None:
    """get_workspace propagates name and description fields from the row."""
    ctx = _ctx(tenant=_TENANT_A, actor=_ACTOR_A)
    ws_row = _make_workspace_row(owner_actor_id=_ACTOR_A)
    ws_row.name = "Special Name"
    ws_row.description = "A description"
    svc = _make_service(workspace_row=ws_row)

    ref = await svc.get_workspace(ctx, _WORKSPACE_ID)

    assert ref.name == "Special Name"
    assert ref.description == "A description"


@pytest.mark.asyncio
async def test_get_workspace_tenant_owned_workspace_accessible_by_tenant_member() -> None:
    """Same-tenant member can get a tenant-owned workspace (path 2: is_same_tenant)."""
    other_actor = uuid.uuid4()
    ctx = _ctx(tenant=_TENANT_A, actor=other_actor)
    ws_row = _make_workspace_row(
        tenant_id=_TENANT_A,
        owner_kind="tenant",
        owner_actor_id=None,
    )
    svc = _make_service(workspace_row=ws_row)

    ref = await svc.get_workspace(ctx, _WORKSPACE_ID)

    assert ref.workspace_id == _WORKSPACE_ID
    assert ref.owner_kind == "tenant"


# ---------------------------------------------------------------------------
# list_workspaces — archived rows handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_workspaces_with_include_archived_returns_archived_rows() -> None:
    """list_workspaces with include_archived=True returns rows where archived_at is set."""
    ctx = _ctx()
    archived_row = _make_workspace_row(archived_at=_NOW)
    svc = _make_service(list_rows=[archived_row])

    refs, _ = await svc.list_workspaces(ctx, include_archived=True)

    assert len(refs) == 1
    assert refs[0].archived_at == _NOW


@pytest.mark.asyncio
async def test_list_workspaces_default_excludes_archived() -> None:
    """list_workspaces returns no rows when the mock DB yields zero results (default excludes archived)."""
    ctx = _ctx()
    svc = _make_service(list_rows=[])

    refs, next_cursor = await svc.list_workspaces(ctx)

    assert refs == []
    assert next_cursor is None


@pytest.mark.asyncio
async def test_list_workspaces_returns_empty_list_when_no_workspaces() -> None:
    """list_workspaces returns ([], None) when the actor has no visible workspaces."""
    ctx = _ctx()
    svc = _make_service(list_rows=[])

    refs, next_cursor = await svc.list_workspaces(ctx)

    assert refs == []
    assert next_cursor is None


# ---------------------------------------------------------------------------
# update_workspace — no fields changed still emits audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_workspace_with_no_fields_changed_emits_audit() -> None:
    """update_workspace emits an audit event even when no fields are changed."""
    ctx = _ctx(tenant=_TENANT_A, actor=_ACTOR_A)
    ws_row = _make_workspace_row(owner_actor_id=_ACTOR_A)
    writer = _audit_writer()
    svc = _make_service(workspace_row=ws_row, audit_writer=writer)

    # Call with no mutation arguments — all kwargs default to None (no change).
    ref = await svc.update_workspace(ctx, _WORKSPACE_ID)

    # Audit is always emitted regardless of whether anything changed.
    writer.emit.assert_awaited()
    last = writer.emit.await_args.kwargs
    assert last["action"] == "workspace.updated"
    # name is preserved from existing row
    assert ref.name == ws_row.name


# ---------------------------------------------------------------------------
# update_workspace — description update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_workspace_description_update() -> None:
    """update_workspace returns the new description in the ref."""
    ctx = _ctx(tenant=_TENANT_A, actor=_ACTOR_A)
    ws_row = _make_workspace_row(owner_actor_id=_ACTOR_A)
    ws_row.description = None
    svc = _make_service(workspace_row=ws_row)

    ref = await svc.update_workspace(ctx, _WORKSPACE_ID, description="New desc")

    assert ref.description == "New desc"


# ---------------------------------------------------------------------------
# update_workspace — tenant admin can update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_workspace_tenant_admin_can_update() -> None:
    """An admin actor in the workspace's tenant can update it even if not the owner."""
    admin_actor = uuid.uuid4()
    ctx = _ctx(tenant=_TENANT_A, actor=admin_actor, roles=["admin"])
    # Workspace is tenant-owned; any admin in the tenant may update.
    ws_row = _make_workspace_row(tenant_id=_TENANT_A, owner_kind="tenant", owner_actor_id=None)
    writer = _audit_writer()
    svc = _make_service(workspace_row=ws_row, actor_roles=["admin"], audit_writer=writer)

    ref = await svc.update_workspace(ctx, _WORKSPACE_ID, name="Admin Renamed")

    assert ref.name == "Admin Renamed"
    writer.emit.assert_awaited()


# ---------------------------------------------------------------------------
# delete_workspace — tenant admin can delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_workspace_tenant_admin_can_delete() -> None:
    """An admin actor in the workspace's tenant can delete a tenant-owned workspace."""
    admin_actor = uuid.uuid4()
    ctx = _ctx(tenant=_TENANT_A, actor=admin_actor, roles=["admin"])
    ws_row = _make_workspace_row(
        tenant_id=_TENANT_A,
        owner_kind="tenant",
        owner_actor_id=None,
        t_invalidated_at=None,
    )
    writer = _audit_writer()
    svc = _make_service(workspace_row=ws_row, actor_roles=["admin"], audit_writer=writer)

    await svc.delete_workspace(ctx, _WORKSPACE_ID)

    writer.emit.assert_awaited_once()
    assert writer.emit.await_args.kwargs["action"] == "workspace.deleted"


# ---------------------------------------------------------------------------
# create_workspace — description field propagated correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_workspace_with_description() -> None:
    """create_workspace propagates an optional description to the returned ref."""
    ctx = _ctx()
    svc = _make_service()

    ref = await svc.create_workspace(
        ctx, name="Described WS", owner_kind="actor", description="My description"
    )

    assert ref.description == "My description"


@pytest.mark.asyncio
async def test_create_workspace_without_description_is_none() -> None:
    """create_workspace returns description=None when no description is supplied."""
    ctx = _ctx()
    svc = _make_service()

    ref = await svc.create_workspace(ctx, name="No Desc WS", owner_kind="actor")

    assert ref.description is None


# ---------------------------------------------------------------------------
# create_workspace — timestamp fields populated from clock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_workspace_timestamps_come_from_clock() -> None:
    """created_at and updated_at in the returned ref equal the clock's now()."""
    ctx = _ctx()
    clock = FakeClock(_NOW)
    svc = _make_service(clock=clock)

    ref = await svc.create_workspace(ctx, name="Timestamped", owner_kind="actor")

    assert ref.created_at == _NOW
    assert ref.updated_at == _NOW


# ---------------------------------------------------------------------------
# list_workspaces — multiple workspaces returned
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_workspaces_returns_multiple_rows() -> None:
    """list_workspaces returns all rows when multiple workspaces are visible."""
    ctx = _ctx()
    rows = _make_ws_rows(5)
    svc = _make_service(list_rows=rows)

    refs, next_cursor = await svc.list_workspaces(ctx)

    assert len(refs) == 5
    assert next_cursor is None


# ---------------------------------------------------------------------------
# update_workspace — updated_at changes in returned ref
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_workspace_updated_at_reflects_clock() -> None:
    """updated_at in the returned ref equals the clock's now() at update time."""
    ctx = _ctx(tenant=_TENANT_A, actor=_ACTOR_A)
    ws_row = _make_workspace_row(owner_actor_id=_ACTOR_A)
    update_time = datetime.datetime(2026, 6, 1, 9, 0, 0, tzinfo=datetime.UTC)
    clock = FakeClock(update_time)
    svc = _make_service(workspace_row=ws_row, clock=clock)

    ref = await svc.update_workspace(ctx, _WORKSPACE_ID, name="Timestamped update")

    assert ref.updated_at == update_time


# ===========================================================================
# purge_actor_personal_data (RTBF) — physical hard-delete
# ===========================================================================
#
# Each test below wires its own AsyncMock session so the SQL routing is
# explicit and independent of the shared _make_session helper (which is
# optimised for create/get/list paths and doesn't route DELETE/rowcount SQL).
# ---------------------------------------------------------------------------


def _make_rtbf_session(
    *,
    entries_rowcount: int = 0,
    cascade_entries_rowcount: int = 0,
    owned_workspace_ids: list[uuid.UUID] | None = None,
    ws_has_other_entries: bool = False,
    track_calls: list[str] | None = None,
) -> AsyncMock:
    """Build a session mock that handles the RTBF purge SQL sequence.

    owned_workspace_ids — workspaces owned by the target actor.
    ws_has_other_entries — if True, the "other actors' entries" check returns a row,
                  triggering the cascade-delete branch.
    entries_rowcount — rows reported by Step 1's DELETE (target actor's entries).
    cascade_entries_rowcount — rows reported by the cascade DELETE of residual
                  entries in an actor-owned workspace before its row is dropped.
    track_calls — if provided, every SQL verb is appended so tests can assert
                  that specific statements were (or were not) executed.
    """
    _track = track_calls if track_calls is not None else []
    _owned_ids = owned_workspace_ids or []

    async def _execute(stmt: Any, params: dict | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        result = MagicMock()

        # Cascade DELETE — residual entries in an actor-owned workspace
        if "DELETE FROM workspace_entries" in sql and "workspace_id = :ws_id" in sql:
            _track.append("DELETE_workspace_entries_cascade")
            result.rowcount = cascade_entries_rowcount
            result.first = MagicMock(return_value=None)
            return result

        # Step 1 — DELETE entries owned by target actor
        if "DELETE FROM workspace_entries" in sql:
            _track.append("DELETE_entries")
            result.rowcount = entries_rowcount
            result.first = MagicMock(return_value=None)
            return result

        # Step 2 — SELECT workspaces owned by target actor
        if "SELECT workspace_id" in sql and "FROM workspaces" in sql and "owner_actor_id = :target_actor_id" in sql:
            _track.append("SELECT_owned_workspaces")
            rows = []
            for ws_id in _owned_ids:
                row = MagicMock()
                row.workspace_id = ws_id
                rows.append(row)
            result.fetchall = MagicMock(return_value=rows)
            return result

        # Step 2 — check for other actors' entries in a workspace
        if "SELECT 1 FROM workspace_entries" in sql and "IS DISTINCT FROM" in sql:
            _track.append("CHECK_other_entries")
            if ws_has_other_entries:
                result.first = MagicMock(return_value=MagicMock())
            else:
                result.first = MagicMock(return_value=None)
            return result

        # Step 2a — DELETE workspace row (only actor's entries remain)
        if "DELETE FROM workspaces" in sql:
            _track.append("DELETE_workspace")
            result.rowcount = 1
            result.first = MagicMock(return_value=None)
            return result

        # Step 2b — UPDATE workspaces (archive + disassociate when others' entries exist)
        if "UPDATE workspaces" in sql and "owner_actor_id = NULL" in sql:
            _track.append("UPDATE_workspace_archive")
            result.rowcount = 1
            result.first = MagicMock(return_value=None)
            return result

        # actor_roles query (used by purge to check caller role)
        if "FROM actor_roles" in sql:
            role_rows = [_make_actor_role_row("admin")]
            result.fetchall = MagicMock(return_value=role_rows)
            result.__iter__ = MagicMock(return_value=iter(role_rows))
            return result

        result.rowcount = 0
        result.first = MagicMock(return_value=None)
        result.fetchall = MagicMock(return_value=[])
        return result

    session = AsyncMock()
    session.execute = _execute
    return session


def _make_rtbf_service(
    *,
    entries_rowcount: int = 0,
    cascade_entries_rowcount: int = 0,
    owned_workspace_ids: list[uuid.UUID] | None = None,
    ws_has_other_entries: bool = False,
    track_calls: list[str] | None = None,
    clock: FakeClock | None = None,
) -> WorkspaceService:
    """Convenience builder for RTBF-focused WorkspaceService fixtures."""
    session = _make_rtbf_session(
        entries_rowcount=entries_rowcount,
        cascade_entries_rowcount=cascade_entries_rowcount,
        owned_workspace_ids=owned_workspace_ids,
        ws_has_other_entries=ws_has_other_entries,
        track_calls=track_calls,
    )
    return WorkspaceService(
        session_factory=_make_factory(session),
        visibility_svc=_visibility(),
        pii_scanner=_pii_clean(),
        audit_writer=_audit_writer(),
        clock=clock or FakeClock(_NOW),
    )


# ---------------------------------------------------------------------------
# (a) Non-admin caller raises 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_purge_rtbf_non_admin_raises_403() -> None:
    """purge_actor_personal_data raises 403 when the caller is not an admin."""
    ctx = _ctx(roles=["producer"])  # no admin role
    svc = _make_rtbf_service()

    with pytest.raises(HTTPException) as exc_info:
        await svc.purge_actor_personal_data(ctx, target_actor_id=uuid.uuid4())

    assert exc_info.value.status_code == 403
    assert "admin" in exc_info.value.detail.lower()


# ---------------------------------------------------------------------------
# (b) Actor with only-owned entries → entries purged, workspace deleted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_purge_rtbf_only_owned_entries_deletes_workspace() -> None:
    """When the actor owns a workspace with only their entries, the workspace is deleted."""
    ctx = _ctx(roles=["admin"])
    target = uuid.uuid4()
    ws_id = uuid.uuid4()
    calls: list[str] = []

    svc = _make_rtbf_service(
        entries_rowcount=3,
        owned_workspace_ids=[ws_id],
        ws_has_other_entries=False,   # only the target actor's entries existed
        track_calls=calls,
    )

    from registry.service.workspace import PurgeResult  # noqa: PLC0415

    result = await svc.purge_actor_personal_data(ctx, target_actor_id=target)

    assert isinstance(result, PurgeResult)
    assert result.purged_entries == 3
    assert result.purged_workspaces == 1   # workspace was deleted (2a path)

    # Step 2a path: workspace row deleted
    assert "DELETE_workspace" in calls
    assert "UPDATE_workspace_archive" not in calls   # 2b path must NOT fire


# ---------------------------------------------------------------------------
# (c) Workspace with other actors' entries → archived, not deleted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_purge_rtbf_owned_workspace_with_other_entries_cascade_deletes() -> None:
    """Residual entries authored by other actors cascade-delete with the actor-owned workspace.

    Actor-owned workspaces are single-writer by design; they cannot survive
    past their owner. Any stray entries authored by other actors (legacy data
    shape that cannot recur) are dropped alongside the workspace and counted
    in purged_entries.
    """
    ctx = _ctx(roles=["admin"])
    target = uuid.uuid4()
    ws_id = uuid.uuid4()
    calls: list[str] = []

    svc = _make_rtbf_service(
        entries_rowcount=1,
        owned_workspace_ids=[ws_id],
        ws_has_other_entries=True,    # another actor has entries → cascade-delete them
        cascade_entries_rowcount=2,   # two residual entries authored by other actors
        track_calls=calls,
    )

    result = await svc.purge_actor_personal_data(ctx, target_actor_id=target)

    # Workspace deleted; residual entries counted against purged_entries
    assert result.purged_workspaces == 1
    assert result.purged_entries == 3   # 1 from Step 1 + 2 from cascade

    assert "DELETE_workspace_entries_cascade" in calls   # cascade fired
    assert "DELETE_workspace" in calls                   # workspace row deleted
    assert "UPDATE_workspace_archive" not in calls       # no preservation path


# ---------------------------------------------------------------------------
# (d) Second purge on same actor → all counts 0 (idempotent)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_purge_rtbf_idempotent_second_run_returns_zero_counts() -> None:
    """A second purge invocation returns all-zero counts (nothing left to purge)."""
    ctx = _ctx(roles=["admin"])
    target = uuid.uuid4()

    # Simulate the state after the first purge: no entries, no owned workspaces.
    svc = _make_rtbf_service(
        entries_rowcount=0,
        owned_workspace_ids=[],
    )

    from registry.service.workspace import PurgeResult  # noqa: PLC0415

    result = await svc.purge_actor_personal_data(ctx, target_actor_id=target)

    assert result.purged_entries == 0
    assert result.purged_workspaces == 0


# ===========================================================================
# Role-visibility matrix — list_workspaces (6 roles × 2 owner_kind)
# ===========================================================================
#
# Each test verifies that list_workspaces issues the SQL query regardless of
# the actor's roles (filtering is pushed into SQL, not Python). The SQL
# visibility predicate contains 'actor_roles' so the DB enforces the role gate.
# We verify that the service correctly forwards whatever rows the DB returns.
# ---------------------------------------------------------------------------


def _make_capturing_list_session(
    *,
    rows: list[MagicMock] | None = None,
    sql_log: list[str] | None = None,
) -> AsyncMock:
    """Session mock that logs SQL and returns 'rows' for list queries."""
    _rows = rows or []
    _log = sql_log if sql_log is not None else []

    async def _execute(stmt: Any, params: dict | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        _log.append(sql)
        result = MagicMock()
        result.fetchall = MagicMock(return_value=_rows)
        result.first = MagicMock(return_value=None)
        return result

    session = AsyncMock()
    session.execute = _execute
    return session


def _list_service_with_roles(roles: list[str], rows: list[MagicMock] | None = None, sql_log: list[str] | None = None) -> WorkspaceService:
    """Build a WorkspaceService whose mock session captures SQL and returns rows."""
    session = _make_capturing_list_session(rows=rows or [], sql_log=sql_log)
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=cm)
    return WorkspaceService(
        session_factory=factory,
        visibility_svc=_visibility(),
        pii_scanner=_pii_clean(),
        audit_writer=_audit_writer(),
        clock=FakeClock(_NOW),
    )


@pytest.mark.asyncio
async def test_list_workspaces_consumer_sees_tenant_ws_when_db_returns_it() -> None:
    """Consumer: DB returns a tenant-owned workspace; list_workspaces surfaces it."""
    ctx = _ctx(roles=["consumer"])
    ws_row = _make_workspace_row(owner_kind="tenant", owner_actor_id=None)
    sql_log: list[str] = []
    svc = _list_service_with_roles(["consumer"], rows=[ws_row], sql_log=sql_log)

    refs, _ = await svc.list_workspaces(ctx)

    assert len(refs) == 1
    assert refs[0].owner_kind == "tenant"
    assert any("actor_roles" in sql for sql in sql_log), "SQL must contain actor_roles predicate"


@pytest.mark.asyncio
async def test_list_workspaces_consumer_sees_own_actor_ws_when_db_returns_it() -> None:
    """Consumer who is the owner: DB returns their actor workspace; list surfaces it."""
    ctx = _ctx(roles=["consumer"])
    ws_row = _make_workspace_row(owner_kind="actor", owner_actor_id=_ACTOR_A)
    sql_log: list[str] = []
    svc = _list_service_with_roles(["consumer"], rows=[ws_row], sql_log=sql_log)

    refs, _ = await svc.list_workspaces(ctx)

    assert len(refs) == 1
    assert any("actor_roles" in sql for sql in sql_log)


@pytest.mark.asyncio
async def test_list_workspaces_producer_sees_tenant_ws_when_db_returns_it() -> None:
    """Producer: DB returns a tenant workspace; list_workspaces surfaces it."""
    ctx = _ctx(roles=["producer"])
    ws_row = _make_workspace_row(owner_kind="tenant", owner_actor_id=None)
    svc = _list_service_with_roles(["producer"], rows=[ws_row])

    refs, _ = await svc.list_workspaces(ctx)

    assert len(refs) == 1


@pytest.mark.asyncio
async def test_list_workspaces_producer_sees_own_actor_ws_when_db_returns_it() -> None:
    """Producer: DB returns their own actor workspace; list_workspaces surfaces it."""
    ctx = _ctx(roles=["producer"])
    ws_row = _make_workspace_row(owner_kind="actor", owner_actor_id=_ACTOR_A)
    svc = _list_service_with_roles(["producer"], rows=[ws_row])

    refs, _ = await svc.list_workspaces(ctx)

    assert len(refs) == 1


@pytest.mark.asyncio
async def test_list_workspaces_admin_pure_sees_tenant_ws_when_db_returns_it() -> None:
    """Pure admin: DB returns a tenant workspace; list_workspaces surfaces it."""
    ctx = _ctx(roles=["admin"])
    ws_row = _make_workspace_row(owner_kind="tenant", owner_actor_id=None)
    svc = _list_service_with_roles(["admin"], rows=[ws_row])

    refs, _ = await svc.list_workspaces(ctx)

    assert len(refs) == 1


@pytest.mark.asyncio
async def test_list_workspaces_admin_pure_no_actor_ws_returned() -> None:
    """Pure admin: DB returns no actor workspaces (SQL predicate excludes them); list is empty."""
    ctx = _ctx(roles=["admin"])
    svc = _list_service_with_roles(["admin"], rows=[])

    refs, _ = await svc.list_workspaces(ctx)

    assert refs == []


@pytest.mark.asyncio
async def test_list_workspaces_admin_producer_sees_tenant_ws() -> None:
    """Admin+Producer: DB returns a tenant workspace; list_workspaces surfaces it."""
    ctx = _ctx(roles=["admin", "producer"])
    ws_row = _make_workspace_row(owner_kind="tenant", owner_actor_id=None)
    svc = _list_service_with_roles(["admin", "producer"], rows=[ws_row])

    refs, _ = await svc.list_workspaces(ctx)

    assert len(refs) == 1


@pytest.mark.asyncio
async def test_list_workspaces_admin_producer_sees_own_actor_ws() -> None:
    """Admin+Producer: DB returns their own actor workspace; list_workspaces surfaces it."""
    ctx = _ctx(roles=["admin", "producer"])
    ws_row = _make_workspace_row(owner_kind="actor", owner_actor_id=_ACTOR_A)
    svc = _list_service_with_roles(["admin", "producer"], rows=[ws_row])

    refs, _ = await svc.list_workspaces(ctx)

    assert len(refs) == 1


@pytest.mark.asyncio
async def test_list_workspaces_auditor_sees_tenant_ws() -> None:
    """Auditor: DB returns a tenant workspace; list_workspaces surfaces it."""
    ctx = _ctx(roles=["auditor"])
    ws_row = _make_workspace_row(owner_kind="tenant", owner_actor_id=None)
    sql_log: list[str] = []
    svc = _list_service_with_roles(["auditor"], rows=[ws_row], sql_log=sql_log)

    refs, _ = await svc.list_workspaces(ctx)

    assert len(refs) == 1
    assert any("actor_roles" in sql for sql in sql_log)


@pytest.mark.asyncio
async def test_list_workspaces_auditor_sees_any_actor_ws() -> None:
    """Auditor: DB returns an actor workspace (audit carve-out); list_workspaces surfaces it."""
    ctx = _ctx(roles=["auditor"])
    other = uuid.uuid4()
    ws_row = _make_workspace_row(owner_kind="actor", owner_actor_id=other)
    svc = _list_service_with_roles(["auditor"], rows=[ws_row])

    refs, _ = await svc.list_workspaces(ctx)

    assert len(refs) == 1


@pytest.mark.asyncio
async def test_list_workspaces_no_role_returns_empty() -> None:
    """No-role actor: DB enforces role gate; list_workspaces returns empty list."""
    ctx = _ctx(roles=[])
    svc = _list_service_with_roles([], rows=[])

    refs, _ = await svc.list_workspaces(ctx)

    assert refs == []


@pytest.mark.asyncio
async def test_list_workspaces_no_role_actor_ws_returns_empty() -> None:
    """No-role actor: DB returns no actor workspaces either; list is empty."""
    ctx = _ctx(roles=[])
    svc = _list_service_with_roles([], rows=[])

    refs, _ = await svc.list_workspaces(ctx)

    assert refs == []


# ===========================================================================
# Role-visibility matrix — search_workspaces (6 roles × 2 owner_kind)
# ===========================================================================
#
# search_workspaces issues a WITH CTE + FROM workspace_entries query.
# The mock session captures the SQL so we can assert the visibility CTE
# contains 'actor_roles'. Result rows simulate entry rows from the entries table.
# ---------------------------------------------------------------------------


def _make_entry_row(workspace_id: uuid.UUID | None = None) -> MagicMock:
    """Build a mock workspace_entries row for search_workspaces results."""
    row = MagicMock()
    row.entry_id = uuid.uuid4()
    row.workspace_id = workspace_id or uuid.uuid4()
    row.tenant_id = _TENANT_A
    row.kind = "note"
    row.body_md = "Search result entry body."
    row.references_jsonb = None
    row.reference_ids = []
    row.expires_at = None
    row.t_invalidated_at = None
    row.created_at = _NOW
    row.updated_at = _NOW
    row.created_by = _ACTOR_A
    return row


def _make_search_session(
    *,
    entry_rows: list[MagicMock] | None = None,
    sql_log: list[str] | None = None,
) -> AsyncMock:
    """Session mock that handles search_workspaces SQL (workspace_entries query)."""
    _rows = entry_rows or []
    _log = sql_log if sql_log is not None else []

    async def _execute(stmt: Any, params: dict | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        _log.append(sql)
        result = MagicMock()
        result.fetchall = MagicMock(return_value=_rows)
        result.first = MagicMock(return_value=None)
        return result

    session = AsyncMock()
    session.execute = _execute
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _search_service_with_roles(roles: list[str], entry_rows: list[MagicMock] | None = None, sql_log: list[str] | None = None) -> WorkspaceService:
    """Build a WorkspaceService whose mock session routes search_workspaces SQL."""
    session = _make_search_session(entry_rows=entry_rows or [], sql_log=sql_log)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=cm)
    return WorkspaceService(
        session_factory=factory,
        visibility_svc=_visibility(),
        pii_scanner=_pii_clean(),
        audit_writer=_audit_writer(),
        clock=FakeClock(_NOW),
    )


@pytest.mark.asyncio
async def test_search_workspaces_consumer_sees_tenant_ws_entries() -> None:
    """Consumer: search returns entries from tenant-owned workspaces when DB returns them."""
    ctx = _ctx(roles=["consumer"])
    entry = _make_entry_row()
    sql_log: list[str] = []
    svc = _search_service_with_roles(["consumer"], entry_rows=[entry], sql_log=sql_log)

    result = await svc.search_workspaces(ctx)

    assert len(result.items) == 1
    assert any("actor_roles" in sql or "visible_workspaces" in sql for sql in sql_log), (
        "search_workspaces must include a visibility CTE with actor_roles"
    )


@pytest.mark.asyncio
async def test_search_workspaces_consumer_actor_ws_entries() -> None:
    """Consumer who owns the workspace: search returns their actor-ws entries."""
    ctx = _ctx(roles=["consumer"])
    entry = _make_entry_row()
    svc = _search_service_with_roles(["consumer"], entry_rows=[entry])

    result = await svc.search_workspaces(ctx)

    assert len(result.items) == 1


@pytest.mark.asyncio
async def test_search_workspaces_producer_sees_entries() -> None:
    """Producer: search returns entries visible to them when DB returns them."""
    ctx = _ctx(roles=["producer"])
    entry = _make_entry_row()
    svc = _search_service_with_roles(["producer"], entry_rows=[entry])

    result = await svc.search_workspaces(ctx)

    assert len(result.items) == 1


@pytest.mark.asyncio
async def test_search_workspaces_producer_actor_ws_entries() -> None:
    """Producer: search returns entries from their own actor workspace."""
    ctx = _ctx(roles=["producer"])
    entry = _make_entry_row()
    svc = _search_service_with_roles(["producer"], entry_rows=[entry])

    result = await svc.search_workspaces(ctx)

    assert len(result.items) == 1


@pytest.mark.asyncio
async def test_search_workspaces_admin_pure_sees_tenant_ws_entries() -> None:
    """Pure admin: search returns entries from tenant workspaces when DB returns them."""
    ctx = _ctx(roles=["admin"])
    entry = _make_entry_row()
    svc = _search_service_with_roles(["admin"], entry_rows=[entry])

    result = await svc.search_workspaces(ctx)

    assert len(result.items) == 1


@pytest.mark.asyncio
async def test_search_workspaces_admin_pure_no_actor_ws_entries() -> None:
    """Pure admin: search returns no actor-ws entries (SQL predicate excludes them)."""
    ctx = _ctx(roles=["admin"])
    svc = _search_service_with_roles(["admin"], entry_rows=[])

    result = await svc.search_workspaces(ctx)

    assert result.items == []


@pytest.mark.asyncio
async def test_search_workspaces_admin_producer_sees_entries() -> None:
    """Admin+Producer: search returns entries from both tenant and actor workspaces."""
    ctx = _ctx(roles=["admin", "producer"])
    entry = _make_entry_row()
    svc = _search_service_with_roles(["admin", "producer"], entry_rows=[entry])

    result = await svc.search_workspaces(ctx)

    assert len(result.items) == 1


@pytest.mark.asyncio
async def test_search_workspaces_admin_producer_own_actor_ws_entries() -> None:
    """Admin+Producer: search surfaces entries from their own actor workspace."""
    ctx = _ctx(roles=["admin", "producer"])
    entry = _make_entry_row()
    svc = _search_service_with_roles(["admin", "producer"], entry_rows=[entry])

    result = await svc.search_workspaces(ctx)

    assert len(result.items) == 1


@pytest.mark.asyncio
async def test_search_workspaces_auditor_sees_tenant_ws_entries() -> None:
    """Auditor: search returns entries from tenant workspaces (auditor can perceive tenant ws)."""
    ctx = _ctx(roles=["auditor"])
    entry = _make_entry_row()
    sql_log: list[str] = []
    svc = _search_service_with_roles(["auditor"], entry_rows=[entry], sql_log=sql_log)

    result = await svc.search_workspaces(ctx)

    assert len(result.items) == 1
    assert any("actor_roles" in sql or "visible_workspaces" in sql for sql in sql_log)


@pytest.mark.asyncio
async def test_search_workspaces_auditor_sees_actor_ws_entries() -> None:
    """Auditor: search returns entries from any actor workspace (audit carve-out)."""
    ctx = _ctx(roles=["auditor"])
    entry = _make_entry_row()
    svc = _search_service_with_roles(["auditor"], entry_rows=[entry])

    result = await svc.search_workspaces(ctx)

    assert len(result.items) == 1


@pytest.mark.asyncio
async def test_search_workspaces_no_role_returns_empty() -> None:
    """No-role actor: DB returns no entries (role gate in SQL); search returns empty."""
    ctx = _ctx(roles=[])
    svc = _search_service_with_roles([], entry_rows=[])

    result = await svc.search_workspaces(ctx)

    assert result.items == []


@pytest.mark.asyncio
async def test_search_workspaces_no_role_actor_ws_returns_empty() -> None:
    """No-role actor: DB returns no actor-ws entries either; search is empty."""
    ctx = _ctx(roles=[])
    svc = _search_service_with_roles([], entry_rows=[])

    result = await svc.search_workspaces(ctx)

    assert result.items == []


# ===========================================================================
# create_workspace — full 12-cell role-check matrix (missing cells)
# ===========================================================================
# The cells for producer/actor, admin/tenant, admin+producer/both, no-role/actor
# are already covered earlier. This section adds the remaining missing cells.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_workspace_consumer_denied_for_actor_kind() -> None:
    """Consumer may not create actor-owned workspaces; only producers may."""
    ctx = _ctx()
    svc = _make_service(actor_roles=["consumer"])

    with pytest.raises(WorkspaceOperationDenied):
        await svc.create_workspace(ctx, name="WS", owner_kind="actor")


@pytest.mark.asyncio
async def test_create_workspace_consumer_denied_for_tenant_kind() -> None:
    """Consumer may not create tenant-owned workspaces; admin role is required."""
    ctx = _ctx()
    svc = _make_service(actor_roles=["consumer"])

    with pytest.raises(WorkspaceOperationDenied):
        await svc.create_workspace(ctx, name="WS", owner_kind="tenant")


@pytest.mark.asyncio
async def test_create_workspace_auditor_denied_for_actor_kind() -> None:
    """Auditor may not create actor-owned workspaces (auditor is read-only)."""
    ctx = _ctx()
    svc = _make_service(actor_roles=["auditor"])

    with pytest.raises(WorkspaceOperationDenied):
        await svc.create_workspace(ctx, name="WS", owner_kind="actor")


@pytest.mark.asyncio
async def test_create_workspace_auditor_denied_for_tenant_kind() -> None:
    """Auditor may not create tenant-owned workspaces (auditor is read-only)."""
    ctx = _ctx()
    svc = _make_service(actor_roles=["auditor"])

    with pytest.raises(WorkspaceOperationDenied):
        await svc.create_workspace(ctx, name="WS", owner_kind="tenant")


@pytest.mark.asyncio
async def test_create_workspace_no_role_denied_for_tenant_kind() -> None:
    """No-role actor may not create tenant-owned workspaces."""
    ctx = _ctx()
    svc = _make_service(actor_roles=[])

    with pytest.raises(WorkspaceOperationDenied):
        await svc.create_workspace(ctx, name="WS", owner_kind="tenant")
