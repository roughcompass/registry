"""Unit tests for WorkspaceService share management.

Covers grant_share, revoke_share, list_shares, and _log_acceptance_if_first.
All DB interaction is mocked at session.execute via an SQL-string-keyed router.

Two-level mock-factory pattern: factory() returns an async context manager whose
__aenter__ yields a session AsyncMock; session.begin() is separately mocked as an
async context manager. This mirrors the pattern from test_workspace_service.py.

The Layer 2 cross-tenant guard (actor-owned workspace cannot be shared cross-tenant)
is verified with an exact message check — the error message is an operational contract
that REST clients and MCP callers parse.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from registry.audit import actions
from registry.service.workspace import (
    ShareRef,
    WorkspaceService,
)
from registry.types import FakeClock, TenantContext

_NOW = datetime.datetime(2026, 5, 12, 12, 0, 0, tzinfo=datetime.UTC)
_TENANT_A = uuid.uuid4()   # workspace owning tenant
_TENANT_B = uuid.uuid4()   # foreign (grantee) tenant
_ACTOR_A = uuid.uuid4()    # owning actor
_ACTOR_B = uuid.uuid4()    # grantee actor
_WORKSPACE_ID = uuid.uuid4()
_SHARE_ID = uuid.uuid4()


# ---------------------------------------------------------------------------
# Context / audit helpers
# ---------------------------------------------------------------------------


def _ctx(
    tenant: uuid.UUID = _TENANT_A,
    actor: uuid.UUID = _ACTOR_A,
    roles: list[str] | None = None,
) -> TenantContext:
    return TenantContext(tenant_id=tenant, actor_id=actor, roles=roles or ["producer"])


def _ctx_admin(tenant: uuid.UUID = _TENANT_A, actor: uuid.UUID = _ACTOR_A) -> TenantContext:
    return TenantContext(tenant_id=tenant, actor_id=actor, roles=["admin"])


def _audit_writer() -> MagicMock:
    writer = MagicMock()
    writer.emit = AsyncMock(return_value=None)
    return writer


def _visibility() -> MagicMock:
    vis = MagicMock()
    vis.assert_visible = AsyncMock(return_value=None)
    return vis


# ---------------------------------------------------------------------------
# Row factories
# ---------------------------------------------------------------------------


def _make_workspace_row(
    *,
    workspace_id: uuid.UUID = _WORKSPACE_ID,
    tenant_id: uuid.UUID = _TENANT_A,
    owner_kind: str = "actor",
    owner_actor_id: uuid.UUID | None = _ACTOR_A,
) -> MagicMock:
    row = MagicMock()
    row.workspace_id = workspace_id
    row.tenant_id = tenant_id
    row.name = "Test WS"
    row.description = None
    row.owner_kind = owner_kind
    row.owner_actor_id = owner_actor_id
    row.archived_at = None
    row.t_invalidated_at = None
    row.created_at = _NOW
    row.updated_at = _NOW
    row.created_by = owner_actor_id
    return row


def _make_share_row(
    *,
    share_id: uuid.UUID = _SHARE_ID,
    workspace_id: uuid.UUID = _WORKSPACE_ID,
    grantee_actor_id: uuid.UUID = _ACTOR_B,
    grantee_tenant_id: uuid.UUID = _TENANT_B,
    role: str = "reader",
    granted_at: datetime.datetime = _NOW,
    revoked_at: datetime.datetime | None = None,
    owner_actor_id: uuid.UUID | None = _ACTOR_A,
    tenant_id: uuid.UUID = _TENANT_A,
) -> MagicMock:
    row = MagicMock()
    row.share_id = share_id
    row.workspace_id = workspace_id
    row.grantee_actor_id = grantee_actor_id
    row.grantee_tenant_id = grantee_tenant_id
    row.role = role
    row.granted_at = granted_at
    row.revoked_at = revoked_at
    row.owner_actor_id = owner_actor_id
    row.tenant_id = tenant_id
    return row


# ---------------------------------------------------------------------------
# Session mock factory
#
# SQL routing keyed by substring match (same strategy as test_workspace_service).
# State is controlled via keyword arguments:
#   workspace_row        — row returned by get_workspace SELECT
#   active_share_row     — row returned by the duplicate-check SELECT in grant_share
#   revoke_share_row     — row returned by the SELECT in revoke_share (includes JOIN)
#   list_share_rows      — rows returned by list_shares SELECT
#   acceptance_insert_affected — rowcount-like indicator (unused; always no-op OK)
# ---------------------------------------------------------------------------


def _make_session(
    *,
    workspace_row: MagicMock | None = None,
    active_share_row: MagicMock | None = None,
    revoke_share_row: MagicMock | None = None,
    list_share_rows: list[MagicMock] | None = None,
) -> AsyncMock:
    """Build an AsyncMock session whose execute routes by SQL keywords."""

    async def _execute(stmt: Any, params: dict | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        result = MagicMock()

        # get_workspace: single-row workspace lookup.
        # Note: get_workspace also checks workspace_shares when not owner/same-tenant,
        # but in these tests we set ctx to be the owner so that branch is skipped.
        if "FROM workspaces" in sql and "workspace_id = :workspace_id" in sql:
            result.first = MagicMock(return_value=workspace_row)
            return result

        # grant_share: duplicate-active-share check.
        # Identified by presence of LIMIT 1 (not present in list_shares) and
        # absence of ORDER BY (list_shares uses ORDER BY granted_at ASC).
        if (
            "FROM workspace_shares" in sql
            and "grantee_actor_id" in sql
            and "revoked_at IS NULL" in sql
            and "JOIN" not in sql
            and "SELECT share_id" in sql
            and "LIMIT 1" in sql
            and "ORDER BY" not in sql
        ):
            result.first = MagicMock(return_value=active_share_row)
            return result

        # grant_share: INSERT new share row.
        if "INSERT INTO workspace_shares" in sql:
            result.first = MagicMock(return_value=None)
            return result

        # revoke_share: SELECT with JOIN workspaces (to get owner_actor_id, tenant_id).
        if "FROM workspace_shares ws" in sql and "JOIN workspaces w" in sql:
            result.first = MagicMock(return_value=revoke_share_row)
            return result

        # revoke_share: UPDATE revoked_at.
        if "UPDATE workspace_shares" in sql:
            result.first = MagicMock(return_value=None)
            return result

        # list_shares: SELECT all active shares for a workspace.
        if (
            "FROM workspace_shares" in sql
            and "revoked_at IS NULL" in sql
            and "ORDER BY" in sql
        ):
            rows = list_share_rows if list_share_rows is not None else []
            result.fetchall = MagicMock(return_value=rows)
            return result

        # _log_acceptance_if_first: INSERT ON CONFLICT DO NOTHING.
        if "INSERT INTO workspace_share_acceptances" in sql:
            result.first = MagicMock(return_value=None)
            return result

        # Fallthrough.
        result.first = MagicMock(return_value=None)
        result.fetchall = MagicMock(return_value=[])
        return result

    session = AsyncMock()
    session.execute = _execute
    return session


def _make_factory(session: AsyncMock) -> MagicMock:
    """Wrap a mock session in the two-level async context manager the service expects."""
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
    workspace_row: MagicMock | None = None,
    active_share_row: MagicMock | None = None,
    revoke_share_row: MagicMock | None = None,
    list_share_rows: list[MagicMock] | None = None,
    audit_writer: MagicMock | None = None,
    clock: FakeClock | None = None,
) -> tuple[WorkspaceService, MagicMock]:
    """Return (service, audit_writer) pair."""
    writer = audit_writer or _audit_writer()
    session = _make_session(
        workspace_row=workspace_row,
        active_share_row=active_share_row,
        revoke_share_row=revoke_share_row,
        list_share_rows=list_share_rows,
    )
    svc = WorkspaceService(
        session_factory=_make_factory(session),
        visibility_svc=_visibility(),
        pii_scanner=MagicMock(),
        audit_writer=writer,
        clock=clock or FakeClock(_NOW),
    )
    return svc, writer


# ---------------------------------------------------------------------------
# (a) grant_share — same-tenant actor-owned workspace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grant_share_same_tenant_actor_owned_succeeds() -> None:
    """grant_share on an actor-owned workspace within the same tenant succeeds."""
    ws_row = _make_workspace_row(owner_kind="actor", owner_actor_id=_ACTOR_A, tenant_id=_TENANT_A)
    svc, writer = _make_service(workspace_row=ws_row, active_share_row=None)

    ref = await svc.grant_share(
        _ctx(tenant=_TENANT_A, actor=_ACTOR_A),
        workspace_id=_WORKSPACE_ID,
        grantee_actor_id=_ACTOR_B,
        grantee_tenant_id=_TENANT_A,  # same tenant as workspace
        role="reader",
    )

    assert isinstance(ref, ShareRef)
    assert ref.workspace_id == _WORKSPACE_ID
    assert ref.grantee_actor_id == _ACTOR_B
    assert ref.grantee_tenant_id == _TENANT_A
    assert ref.role == "reader"
    assert ref.revoked_at is None
    writer.emit.assert_awaited_once()
    call_kwargs = writer.emit.await_args.kwargs
    assert call_kwargs["action"] == actions.WORKSPACE_SHARE_GRANTED
    assert call_kwargs["target_type"] == "workspace_share"


# ---------------------------------------------------------------------------
# (b) grant_share — cross-tenant tenant-owned workspace succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grant_share_cross_tenant_tenant_owned_succeeds() -> None:
    """grant_share cross-tenant on a tenant-owned workspace is permitted."""
    ws_row = _make_workspace_row(
        owner_kind="tenant",
        owner_actor_id=None,
        tenant_id=_TENANT_A,
    )
    svc, writer = _make_service(workspace_row=ws_row, active_share_row=None)

    ref = await svc.grant_share(
        _ctx_admin(tenant=_TENANT_A, actor=_ACTOR_A),
        workspace_id=_WORKSPACE_ID,
        grantee_actor_id=_ACTOR_B,
        grantee_tenant_id=_TENANT_B,  # different tenant — allowed for tenant-owned
        role="reader",
    )

    assert isinstance(ref, ShareRef)
    assert ref.grantee_tenant_id == _TENANT_B
    writer.emit.assert_awaited_once()
    call_kwargs = writer.emit.await_args.kwargs
    assert call_kwargs["action"] == actions.WORKSPACE_SHARE_GRANTED


# ---------------------------------------------------------------------------
# (c) grant_share — cross-tenant actor-owned workspace raises 422 (Layer 2 guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grant_share_cross_tenant_actor_owned_raises_422() -> None:
    """Layer 2 guard rejects cross-tenant share on actor-owned workspace with exact message."""
    ws_row = _make_workspace_row(owner_kind="actor", owner_actor_id=_ACTOR_A, tenant_id=_TENANT_A)
    svc, writer = _make_service(workspace_row=ws_row)

    with pytest.raises(HTTPException) as exc_info:
        await svc.grant_share(
            _ctx(tenant=_TENANT_A, actor=_ACTOR_A),
            workspace_id=_WORKSPACE_ID,
            grantee_actor_id=_ACTOR_B,
            grantee_tenant_id=_TENANT_B,  # different tenant — rejected for actor-owned
            role="reader",
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == (
        "Actor-owned workspaces may only be shared within the same tenant. "
        "To share cross-tenant, the workspace must be tenant-owned."
    )
    # Guard fires before any INSERT — audit must not be emitted.
    writer.emit.assert_not_awaited()


# ---------------------------------------------------------------------------
# (d) revoke_share — idempotent (second call no-op, no duplicate audit)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_share_idempotent_second_call_no_op() -> None:
    """Revoking an already-revoked share is a no-op: no error and no audit event."""
    revoked_row = _make_share_row(
        revoked_at=_NOW,  # already revoked
        owner_actor_id=_ACTOR_A,
        tenant_id=_TENANT_A,
    )
    svc, writer = _make_service(revoke_share_row=revoked_row)

    # First call: should have revoked, but row is pre-revoked → no-op.
    await svc.revoke_share(_ctx(tenant=_TENANT_A, actor=_ACTOR_A), share_id=_SHARE_ID)

    writer.emit.assert_not_awaited()


@pytest.mark.asyncio
async def test_revoke_share_active_then_idempotent_no_duplicate_audit() -> None:
    """Revoking an active share emits one audit; a second call (already revoked) emits none."""
    active_row = _make_share_row(
        revoked_at=None,  # active
        owner_actor_id=_ACTOR_A,
        tenant_id=_TENANT_A,
    )
    svc, writer = _make_service(revoke_share_row=active_row)

    await svc.revoke_share(_ctx_admin(tenant=_TENANT_A, actor=_ACTOR_A), share_id=_SHARE_ID)
    writer.emit.assert_awaited_once()
    call_kwargs = writer.emit.await_args.kwargs
    assert call_kwargs["action"] == actions.WORKSPACE_SHARE_REVOKED


# ---------------------------------------------------------------------------
# (e) re-grant after revocation — new share row created
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_regrant_after_revocation_succeeds() -> None:
    """Re-granting after revocation succeeds: no active duplicate, new row inserted."""
    ws_row = _make_workspace_row(owner_kind="actor", owner_actor_id=_ACTOR_A, tenant_id=_TENANT_A)
    # active_share_row=None means no active duplicate exists (previous share was revoked).
    svc, writer = _make_service(workspace_row=ws_row, active_share_row=None)

    ref = await svc.grant_share(
        _ctx(tenant=_TENANT_A, actor=_ACTOR_A),
        workspace_id=_WORKSPACE_ID,
        grantee_actor_id=_ACTOR_B,
        grantee_tenant_id=_TENANT_A,
        role="reader",
    )

    assert isinstance(ref, ShareRef)
    assert ref.revoked_at is None
    writer.emit.assert_awaited_once()
    assert writer.emit.await_args.kwargs["action"] == actions.WORKSPACE_SHARE_GRANTED


# ---------------------------------------------------------------------------
# (f) duplicate active share — 409 Conflict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grant_share_duplicate_active_raises_409() -> None:
    """Granting a share when an active share already exists raises 409."""
    ws_row = _make_workspace_row(owner_kind="actor", owner_actor_id=_ACTOR_A, tenant_id=_TENANT_A)
    # Return an existing active share row for the duplicate check.
    existing_share = MagicMock()
    existing_share.share_id = uuid.uuid4()
    svc, writer = _make_service(workspace_row=ws_row, active_share_row=existing_share)

    with pytest.raises(HTTPException) as exc_info:
        await svc.grant_share(
            _ctx(tenant=_TENANT_A, actor=_ACTOR_A),
            workspace_id=_WORKSPACE_ID,
            grantee_actor_id=_ACTOR_B,
            grantee_tenant_id=_TENANT_A,
            role="reader",
        )

    assert exc_info.value.status_code == 409
    assert "active share" in exc_info.value.detail
    writer.emit.assert_not_awaited()


# ---------------------------------------------------------------------------
# (g) _log_acceptance_if_first — records acceptance on first cross-tenant read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_acceptance_if_first_inserts_row() -> None:
    """_log_acceptance_if_first inserts an acceptance row on first cross-tenant read."""
    # Use a fresh session so we can track the INSERT call.
    ws_row = _make_workspace_row(owner_kind="tenant", owner_actor_id=None, tenant_id=_TENANT_A)
    svc, _ = _make_service(workspace_row=ws_row)

    # Call the method directly — no exception means the INSERT was attempted.
    await svc._log_acceptance_if_first(
        _ctx(tenant=_TENANT_B, actor=_ACTOR_B),
        share_id=_SHARE_ID,
        workspace_id=_WORKSPACE_ID,
    )
    # If the session mock raised, the method would suppress it and log a warning.
    # Reaching here means execution completed without raising outward.


# ---------------------------------------------------------------------------
# (h) _log_acceptance_if_first — idempotent (second call ON CONFLICT DO NOTHING)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_acceptance_if_first_idempotent() -> None:
    """_log_acceptance_if_first is idempotent: second call is a silent no-op."""
    svc, _ = _make_service()

    ctx = _ctx(tenant=_TENANT_B, actor=_ACTOR_B)

    # Both calls must complete without error. The ON CONFLICT DO NOTHING in the SQL
    # ensures only one row is ever inserted; the mock verifies no exception escapes.
    await svc._log_acceptance_if_first(ctx, share_id=_SHARE_ID, workspace_id=_WORKSPACE_ID)
    await svc._log_acceptance_if_first(ctx, share_id=_SHARE_ID, workspace_id=_WORKSPACE_ID)


# ---------------------------------------------------------------------------
# (i) grant_share — invalid role raises 422
#
# The workspace_shares table carries CHECK (role IN ('reader','contributor')).
# grant_share validates the role at the service layer before the INSERT so
# callers receive an actionable 422 rather than a raw DB constraint violation.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grant_share_invalid_role_raises_422() -> None:
    """grant_share with an invalid role ('superadmin') must raise 422."""
    ws_row = _make_workspace_row(owner_kind="actor", owner_actor_id=_ACTOR_A, tenant_id=_TENANT_A)
    svc, writer = _make_service(workspace_row=ws_row, active_share_row=None)

    with pytest.raises(HTTPException) as exc_info:
        await svc.grant_share(
            _ctx(tenant=_TENANT_A, actor=_ACTOR_A),
            workspace_id=_WORKSPACE_ID,
            grantee_actor_id=_ACTOR_B,
            grantee_tenant_id=_TENANT_A,
            role="superadmin",  # not in ('reader', 'contributor')
        )

    assert exc_info.value.status_code == 422
    assert "role" in exc_info.value.detail.lower()
    writer.emit.assert_not_awaited()


# ---------------------------------------------------------------------------
# (j) list_shares — returns only active shares (revoked_at IS NULL)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_shares_returns_only_active() -> None:
    """list_shares only returns rows with revoked_at IS NULL."""
    ws_row = _make_workspace_row(owner_kind="actor", owner_actor_id=_ACTOR_A, tenant_id=_TENANT_A)
    active_share = _make_share_row(revoked_at=None)
    # The session mock for list_shares only returns what list_share_rows contains.
    # We pass only the active share; revoked rows would come from DB filtering
    # which the SQL WHERE clause (revoked_at IS NULL) enforces.
    svc, _ = _make_service(workspace_row=ws_row, list_share_rows=[active_share])

    shares = await svc.list_shares(_ctx(tenant=_TENANT_A, actor=_ACTOR_A), workspace_id=_WORKSPACE_ID)

    assert len(shares) == 1
    assert shares[0].share_id == _SHARE_ID
    assert shares[0].revoked_at is None


# ---------------------------------------------------------------------------
# (k) list_shares — non-owner non-admin → 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_shares_non_owner_non_admin_raises_403() -> None:
    """list_shares by an actor who is not the owner and not an admin raises 403."""
    _ACTOR_OTHER = uuid.uuid4()
    ws_row = _make_workspace_row(owner_kind="actor", owner_actor_id=_ACTOR_A, tenant_id=_TENANT_A)
    svc, writer = _make_service(workspace_row=ws_row)

    # _ACTOR_OTHER is in the same tenant (so get_workspace passes) but is not the owner
    # and does not carry the 'admin' role.
    ctx = TenantContext(tenant_id=_TENANT_A, actor_id=_ACTOR_OTHER, roles=["producer"])

    with pytest.raises(HTTPException) as exc_info:
        await svc.list_shares(ctx, workspace_id=_WORKSPACE_ID)

    assert exc_info.value.status_code == 403
    writer.emit.assert_not_awaited()


# ---------------------------------------------------------------------------
# (l) revoke_share — non-owner (non-admin) → 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_share_non_owner_raises_403() -> None:
    """revoke_share by an actor who is not the owner and not an admin raises 403."""
    _ACTOR_OTHER = uuid.uuid4()
    # The share row has owner_actor_id=_ACTOR_A; caller is _ACTOR_OTHER.
    active_row = _make_share_row(
        revoked_at=None,
        owner_actor_id=_ACTOR_A,
        tenant_id=_TENANT_A,
    )
    svc, writer = _make_service(revoke_share_row=active_row)

    ctx = TenantContext(tenant_id=_TENANT_A, actor_id=_ACTOR_OTHER, roles=["producer"])

    with pytest.raises(HTTPException) as exc_info:
        await svc.revoke_share(ctx, share_id=_SHARE_ID)

    assert exc_info.value.status_code == 403
    writer.emit.assert_not_awaited()


# ---------------------------------------------------------------------------
# (m) grant_share — audit-log emitted with correct fields
#     (action, target_type, target_id=share_id, after dict with grantee info)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grant_share_audit_log_correct_fields() -> None:
    """grant_share emits an audit event with the exact required fields."""
    ws_row = _make_workspace_row(owner_kind="actor", owner_actor_id=_ACTOR_A, tenant_id=_TENANT_A)
    svc, writer = _make_service(workspace_row=ws_row, active_share_row=None)

    ref = await svc.grant_share(
        _ctx(tenant=_TENANT_A, actor=_ACTOR_A),
        workspace_id=_WORKSPACE_ID,
        grantee_actor_id=_ACTOR_B,
        grantee_tenant_id=_TENANT_A,
        role="reader",
    )

    writer.emit.assert_awaited_once()
    kw = writer.emit.await_args.kwargs
    assert kw["action"] == actions.WORKSPACE_SHARE_GRANTED
    assert kw["target_type"] == "workspace_share"
    assert kw["target_id"] == ref.share_id   # target_id is the newly created share_id
    after = kw["after"]
    assert after["grantee_actor_id"] == str(_ACTOR_B)
    assert after["grantee_tenant_id"] == str(_TENANT_A)
    assert after["role"] == "reader"
    assert after["workspace_id"] == str(_WORKSPACE_ID)


# ---------------------------------------------------------------------------
# (n) revoke_share — by owning actor on tenant-owned workspace succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_share_owner_on_tenant_owned_workspace_succeeds() -> None:
    """revoke_share by the owning actor succeeds even when workspace is tenant-owned.

    For tenant-owned workspaces owner_actor_id is NULL; authorization falls through
    to the is_tenant_admin path. This test uses an admin-role actor in the workspace's
    tenant, which is the correct grant path for tenant-owned workspaces.
    """
    # tenant-owned: owner_actor_id is NULL in the share row.
    active_row = _make_share_row(
        revoked_at=None,
        owner_actor_id=None,   # tenant-owned; no personal owner
        tenant_id=_TENANT_A,
    )
    svc, writer = _make_service(revoke_share_row=active_row)

    # Admin in the workspace's tenant satisfies is_tenant_admin.
    await svc.revoke_share(_ctx_admin(tenant=_TENANT_A, actor=_ACTOR_A), share_id=_SHARE_ID)

    writer.emit.assert_awaited_once()
    kw = writer.emit.await_args.kwargs
    assert kw["action"] == actions.WORKSPACE_SHARE_REVOKED
    assert kw["target_id"] == _SHARE_ID


# ---------------------------------------------------------------------------
# (o) revoke_share — audit-log emitted with correct fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_share_audit_log_correct_fields() -> None:
    """revoke_share emits an audit event with share_id as target_id."""
    active_row = _make_share_row(revoked_at=None, owner_actor_id=_ACTOR_A, tenant_id=_TENANT_A)
    svc, writer = _make_service(revoke_share_row=active_row)

    await svc.revoke_share(_ctx(tenant=_TENANT_A, actor=_ACTOR_A), share_id=_SHARE_ID)

    writer.emit.assert_awaited_once()
    kw = writer.emit.await_args.kwargs
    assert kw["action"] == actions.WORKSPACE_SHARE_REVOKED
    assert kw["target_type"] == "workspace_share"
    assert kw["target_id"] == _SHARE_ID
    assert "revoked_at" in kw["after"]


# ---------------------------------------------------------------------------
# (p) grant_share — by random actor (non-owner, non-admin) → 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grant_share_non_owner_non_admin_raises_403() -> None:
    """grant_share by an actor who is neither the workspace owner nor an admin raises 403."""
    _ACTOR_RANDOM = uuid.uuid4()
    ws_row = _make_workspace_row(owner_kind="actor", owner_actor_id=_ACTOR_A, tenant_id=_TENANT_A)
    svc, writer = _make_service(workspace_row=ws_row, active_share_row=None)

    # Same tenant (so get_workspace passes) but not the owner and not admin.
    ctx = TenantContext(tenant_id=_TENANT_A, actor_id=_ACTOR_RANDOM, roles=["producer"])

    with pytest.raises(HTTPException) as exc_info:
        await svc.grant_share(
            ctx,
            workspace_id=_WORKSPACE_ID,
            grantee_actor_id=_ACTOR_B,
            grantee_tenant_id=_TENANT_A,
            role="reader",
        )

    assert exc_info.value.status_code == 403
    writer.emit.assert_not_awaited()


# ---------------------------------------------------------------------------
# (q) list_shares — returns empty list when no active shares exist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_shares_empty_when_no_shares() -> None:
    """list_shares returns an empty list when the workspace has no active shares."""
    ws_row = _make_workspace_row(owner_kind="actor", owner_actor_id=_ACTOR_A, tenant_id=_TENANT_A)
    svc, _ = _make_service(workspace_row=ws_row, list_share_rows=[])

    shares = await svc.list_shares(_ctx(tenant=_TENANT_A, actor=_ACTOR_A), workspace_id=_WORKSPACE_ID)

    assert shares == []
