"""Unit tests for workspace RBAC predicate functions.

These are pure-function tests — no DB session, no HTTP client, no async loop.
Each test covers one cell from the authorization matrix. The matrix cells are:
  - _can_perceive_workspace: 31 cases (cross-tenant, no-role, invalidated, role x owner_kind)
  - _assert_can_write_entries: 12 cases
  - _assert_can_update_workspace: 12 cases (identical denial table to write_entries)
  - _assert_can_delete_workspace: 12 cases (archive-state-independent)
  - _assert_can_archive_workspace: 10 cases (input state is by definition unarchived)
  - Auditor write-attempt cases: 5 explicit cases
  - Invalidated-workspace cases: 6 explicit cases

All helpers raise WorkspaceOperationDenied on denial and never raise WorkspaceNotFound.
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from registry.service.workspace import (
    WorkspaceOperationDenied,
    WorkspaceRef,
    _assert_can_archive_workspace,
    _assert_can_delete_workspace,
    _assert_can_update_workspace,
    _assert_can_write_entries,
    _can_perceive_workspace,
)

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)
_ARCHIVED = datetime.datetime(2026, 1, 2, 12, 0, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Shared fixtures for workspace row stubs
# ---------------------------------------------------------------------------


def _make_ws(
    *,
    owner_kind: str = "actor",
    owner_actor_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    archived_at: datetime.datetime | None = None,
    t_invalidated_at: datetime.datetime | None = None,
) -> WorkspaceRef:
    """Build a minimal WorkspaceRef for predicate testing."""
    ws_tenant_id = tenant_id or uuid.uuid4()
    return WorkspaceRef(
        workspace_id=uuid.uuid4(),
        tenant_id=ws_tenant_id,
        name="test-workspace",
        description=None,
        owner_kind=owner_kind,
        owner_actor_id=owner_actor_id,
        archived_at=archived_at,
        created_at=_NOW,
        updated_at=_NOW,
        created_by=owner_actor_id,
        t_invalidated_at=t_invalidated_at,
    )


# ---------------------------------------------------------------------------
# _can_perceive_workspace — cross-tenant cases
# ---------------------------------------------------------------------------


def test_perceive_cross_tenant_producer_actor_ws() -> None:
    """Producer in tenant A cannot perceive actor workspace in tenant B."""
    actor_id = uuid.uuid4()
    tenant_a = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=actor_id)
    # ws.tenant_id != tenant_a
    assert not _can_perceive_workspace(frozenset({"producer"}), actor_id, tenant_a, ws)


def test_perceive_cross_tenant_consumer_tenant_ws() -> None:
    """Consumer in tenant A cannot perceive tenant workspace in tenant B."""
    actor_id = uuid.uuid4()
    tenant_a = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant")
    assert not _can_perceive_workspace(frozenset({"consumer"}), actor_id, tenant_a, ws)


def test_perceive_cross_tenant_producer_tenant_ws() -> None:
    """Producer in tenant A cannot perceive tenant workspace in tenant B."""
    actor_id = uuid.uuid4()
    tenant_a = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant")
    assert not _can_perceive_workspace(frozenset({"producer"}), actor_id, tenant_a, ws)


def test_perceive_cross_tenant_admin_tenant_ws() -> None:
    """Pure admin in tenant A cannot perceive tenant workspace in tenant B."""
    actor_id = uuid.uuid4()
    tenant_a = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant")
    assert not _can_perceive_workspace(frozenset({"admin"}), actor_id, tenant_a, ws)


def test_perceive_cross_tenant_admin_producer_tenant_ws() -> None:
    """Admin+Producer in tenant A cannot perceive tenant workspace in tenant B."""
    actor_id = uuid.uuid4()
    tenant_a = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant")
    assert not _can_perceive_workspace(frozenset({"admin", "producer"}), actor_id, tenant_a, ws)


def test_perceive_cross_tenant_auditor_tenant_ws() -> None:
    """Auditor in tenant A cannot perceive tenant workspace in tenant B."""
    actor_id = uuid.uuid4()
    tenant_a = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant")
    assert not _can_perceive_workspace(frozenset({"auditor"}), actor_id, tenant_a, ws)


def test_perceive_cross_tenant_auditor_actor_ws() -> None:
    """Auditor in tenant A cannot perceive actor workspace in tenant B."""
    actor_id = uuid.uuid4()
    tenant_a = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=uuid.uuid4())
    assert not _can_perceive_workspace(frozenset({"auditor"}), actor_id, tenant_a, ws)


def test_perceive_cross_tenant_no_role_tenant_ws() -> None:
    """No-role actor in tenant A cannot perceive tenant workspace in tenant B."""
    actor_id = uuid.uuid4()
    tenant_a = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant")
    assert not _can_perceive_workspace(frozenset(), actor_id, tenant_a, ws)


# ---------------------------------------------------------------------------
# _can_perceive_workspace — no-role cases
# ---------------------------------------------------------------------------


def test_perceive_no_role_tenant_ws() -> None:
    """Actor with no roles cannot perceive a tenant workspace."""
    actor_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant", tenant_id=tenant_id)
    assert not _can_perceive_workspace(frozenset(), actor_id, tenant_id, ws)


def test_perceive_no_role_own_actor_ws() -> None:
    """Actor with no roles cannot perceive their own actor workspace."""
    actor_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=actor_id, tenant_id=tenant_id)
    assert not _can_perceive_workspace(frozenset(), actor_id, tenant_id, ws)


def test_perceive_no_role_other_actor_ws() -> None:
    """Actor with no roles cannot perceive another actor's workspace."""
    actor_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    other = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=other, tenant_id=tenant_id)
    assert not _can_perceive_workspace(frozenset(), actor_id, tenant_id, ws)


# ---------------------------------------------------------------------------
# _can_perceive_workspace — invalidated workspace cases (step 3 fires first)
# ---------------------------------------------------------------------------


def test_perceive_invalidated_tenant_ws_any_role() -> None:
    """Producer cannot perceive an invalidated (soft-deleted) actor workspace."""
    actor_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    ws = _make_ws(
        owner_kind="actor",
        owner_actor_id=actor_id,
        tenant_id=tenant_id,
        t_invalidated_at=_NOW,
    )
    assert not _can_perceive_workspace(frozenset({"producer"}), actor_id, tenant_id, ws)


def test_perceive_invalidated_actor_ws_auditor() -> None:
    """Auditor cannot perceive an invalidated actor workspace."""
    actor_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    other = uuid.uuid4()
    ws = _make_ws(
        owner_kind="actor",
        owner_actor_id=other,
        tenant_id=tenant_id,
        t_invalidated_at=_NOW,
    )
    assert not _can_perceive_workspace(frozenset({"auditor"}), actor_id, tenant_id, ws)


# ---------------------------------------------------------------------------
# _can_perceive_workspace — tenant workspace cases
# ---------------------------------------------------------------------------


def test_perceive_consumer_tenant_ws_active() -> None:
    """Consumer perceives an active tenant workspace."""
    actor_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant", tenant_id=tenant_id)
    assert _can_perceive_workspace(frozenset({"consumer"}), actor_id, tenant_id, ws)


def test_perceive_consumer_tenant_ws_archived() -> None:
    """Consumer perceives an archived (but not invalidated) tenant workspace."""
    actor_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant", tenant_id=tenant_id, archived_at=_ARCHIVED)
    assert _can_perceive_workspace(frozenset({"consumer"}), actor_id, tenant_id, ws)


def test_perceive_producer_tenant_ws() -> None:
    """Producer perceives an active tenant workspace."""
    actor_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant", tenant_id=tenant_id)
    assert _can_perceive_workspace(frozenset({"producer"}), actor_id, tenant_id, ws)


def test_perceive_admin_pure_tenant_ws() -> None:
    """Pure admin perceives a tenant workspace."""
    actor_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant", tenant_id=tenant_id)
    assert _can_perceive_workspace(frozenset({"admin"}), actor_id, tenant_id, ws)


def test_perceive_admin_producer_tenant_ws() -> None:
    """Admin+Producer perceives a tenant workspace."""
    actor_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant", tenant_id=tenant_id)
    assert _can_perceive_workspace(frozenset({"admin", "producer"}), actor_id, tenant_id, ws)


def test_perceive_auditor_tenant_ws() -> None:
    """Auditor perceives a tenant workspace."""
    actor_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant", tenant_id=tenant_id)
    assert _can_perceive_workspace(frozenset({"auditor"}), actor_id, tenant_id, ws)


# ---------------------------------------------------------------------------
# _can_perceive_workspace — actor workspace cases
# ---------------------------------------------------------------------------


def test_perceive_consumer_no_ownership_actor_ws() -> None:
    """Consumer cannot perceive another actor's workspace (no ownership)."""
    actor_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    other = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=other, tenant_id=tenant_id)
    assert not _can_perceive_workspace(frozenset({"consumer"}), actor_id, tenant_id, ws)


def test_perceive_consumer_own_actor_ws() -> None:
    """Consumer who is the owner can perceive their own actor workspace."""
    actor_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=actor_id, tenant_id=tenant_id)
    assert _can_perceive_workspace(frozenset({"consumer"}), actor_id, tenant_id, ws)


def test_perceive_producer_own_actor_ws_active() -> None:
    """Producer perceives their own active actor workspace."""
    actor_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=actor_id, tenant_id=tenant_id)
    assert _can_perceive_workspace(frozenset({"producer"}), actor_id, tenant_id, ws)


def test_perceive_producer_own_actor_ws_archived() -> None:
    """Producer perceives their own archived (but not invalidated) actor workspace."""
    actor_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    ws = _make_ws(
        owner_kind="actor",
        owner_actor_id=actor_id,
        tenant_id=tenant_id,
        archived_at=_ARCHIVED,
    )
    assert _can_perceive_workspace(frozenset({"producer"}), actor_id, tenant_id, ws)


def test_perceive_producer_other_actor_ws() -> None:
    """Producer cannot perceive another actor's workspace."""
    actor_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    other = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=other, tenant_id=tenant_id)
    assert not _can_perceive_workspace(frozenset({"producer"}), actor_id, tenant_id, ws)


def test_perceive_admin_pure_own_actor_ws() -> None:
    """Pure admin cannot perceive an actor workspace (even their own)."""
    actor_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=actor_id, tenant_id=tenant_id)
    assert not _can_perceive_workspace(frozenset({"admin"}), actor_id, tenant_id, ws)


def test_perceive_admin_pure_other_actor_ws() -> None:
    """Pure admin cannot perceive another actor's workspace."""
    actor_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    other = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=other, tenant_id=tenant_id)
    assert not _can_perceive_workspace(frozenset({"admin"}), actor_id, tenant_id, ws)


def test_perceive_admin_producer_own_actor_ws() -> None:
    """Admin+Producer perceives their own actor workspace (producer carve-out)."""
    actor_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=actor_id, tenant_id=tenant_id)
    assert _can_perceive_workspace(frozenset({"admin", "producer"}), actor_id, tenant_id, ws)


def test_perceive_admin_producer_other_actor_ws() -> None:
    """Admin+Producer cannot perceive another actor's workspace."""
    actor_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    other = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=other, tenant_id=tenant_id)
    assert not _can_perceive_workspace(frozenset({"admin", "producer"}), actor_id, tenant_id, ws)


def test_perceive_auditor_any_actor_ws() -> None:
    """Auditor perceives any actor workspace (audit carve-out)."""
    actor_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    other = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=other, tenant_id=tenant_id)
    assert _can_perceive_workspace(frozenset({"auditor"}), actor_id, tenant_id, ws)


def test_perceive_auditor_archived_actor_ws() -> None:
    """Auditor perceives an archived (but not invalidated) actor workspace."""
    actor_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    other = uuid.uuid4()
    ws = _make_ws(
        owner_kind="actor",
        owner_actor_id=other,
        tenant_id=tenant_id,
        archived_at=_ARCHIVED,
    )
    assert _can_perceive_workspace(frozenset({"auditor"}), actor_id, tenant_id, ws)


# ---------------------------------------------------------------------------
# _can_perceive_workspace — invalidated workspace: all roles return False (§5.1b)
# ---------------------------------------------------------------------------


def test_perceive_invalidated_consumer() -> None:
    """Consumer cannot perceive an invalidated tenant workspace."""
    actor_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant", tenant_id=tenant_id, t_invalidated_at=_NOW)
    assert not _can_perceive_workspace(frozenset({"consumer"}), actor_id, tenant_id, ws)


def test_perceive_invalidated_producer_own() -> None:
    """Producer cannot perceive their own invalidated actor workspace."""
    actor_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    ws = _make_ws(
        owner_kind="actor",
        owner_actor_id=actor_id,
        tenant_id=tenant_id,
        t_invalidated_at=_NOW,
    )
    assert not _can_perceive_workspace(frozenset({"producer"}), actor_id, tenant_id, ws)


def test_perceive_invalidated_admin_pure() -> None:
    """Pure admin cannot perceive an invalidated tenant workspace."""
    actor_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant", tenant_id=tenant_id, t_invalidated_at=_NOW)
    assert not _can_perceive_workspace(frozenset({"admin"}), actor_id, tenant_id, ws)


def test_perceive_invalidated_admin_producer_own() -> None:
    """Admin+Producer cannot perceive their own invalidated actor workspace."""
    actor_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    ws = _make_ws(
        owner_kind="actor",
        owner_actor_id=actor_id,
        tenant_id=tenant_id,
        t_invalidated_at=_NOW,
    )
    assert not _can_perceive_workspace(frozenset({"admin", "producer"}), actor_id, tenant_id, ws)


def test_perceive_invalidated_auditor_any() -> None:
    """Auditor cannot perceive an invalidated actor workspace (invalidated check fires first)."""
    actor_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    other = uuid.uuid4()
    ws = _make_ws(
        owner_kind="actor",
        owner_actor_id=other,
        tenant_id=tenant_id,
        t_invalidated_at=_NOW,
    )
    assert not _can_perceive_workspace(frozenset({"auditor"}), actor_id, tenant_id, ws)


def test_perceive_invalidated_no_role() -> None:
    """No-role actor cannot perceive an invalidated tenant workspace."""
    actor_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant", tenant_id=tenant_id, t_invalidated_at=_NOW)
    assert not _can_perceive_workspace(frozenset(), actor_id, tenant_id, ws)


# ---------------------------------------------------------------------------
# _assert_can_write_entries — 12 cells
# ---------------------------------------------------------------------------


def test_write_entries_producer_own_actor_ws_active() -> None:
    """Producer writing entries on their own active actor workspace: allowed."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=actor_id)
    _assert_can_write_entries(frozenset({"producer"}), actor_id, ws)


def test_write_entries_producer_own_actor_ws_archived() -> None:
    """Producer writing entries on their own archived actor workspace: denied."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=actor_id, archived_at=_ARCHIVED)
    with pytest.raises(WorkspaceOperationDenied):
        _assert_can_write_entries(frozenset({"producer"}), actor_id, ws)


def test_write_entries_producer_tenant_ws() -> None:
    """Producer writing entries on a tenant workspace: denied (requires admin)."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant")
    with pytest.raises(WorkspaceOperationDenied):
        _assert_can_write_entries(frozenset({"producer"}), actor_id, ws)


def test_write_entries_admin_tenant_ws_active() -> None:
    """Admin writing entries on an active tenant workspace: allowed."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant")
    _assert_can_write_entries(frozenset({"admin"}), actor_id, ws)


def test_write_entries_admin_tenant_ws_archived() -> None:
    """Admin writing entries on an archived tenant workspace: denied."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant", archived_at=_ARCHIVED)
    with pytest.raises(WorkspaceOperationDenied):
        _assert_can_write_entries(frozenset({"admin"}), actor_id, ws)


def test_write_entries_consumer_own_actor_ws() -> None:
    """Consumer writing entries on their own actor workspace: denied (consumer cannot write)."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=actor_id)
    with pytest.raises(WorkspaceOperationDenied):
        _assert_can_write_entries(frozenset({"consumer"}), actor_id, ws)


def test_write_entries_auditor_any_actor_ws() -> None:
    """Auditor writing entries on any actor workspace: denied."""
    actor_id = uuid.uuid4()
    other = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=other)
    with pytest.raises(WorkspaceOperationDenied):
        _assert_can_write_entries(frozenset({"auditor"}), actor_id, ws)


def test_write_entries_auditor_tenant_ws() -> None:
    """Auditor writing entries on a tenant workspace: denied."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant")
    with pytest.raises(WorkspaceOperationDenied):
        _assert_can_write_entries(frozenset({"auditor"}), actor_id, ws)


def test_write_entries_admin_producer_own_actor_ws() -> None:
    """Admin+Producer writing entries on their own actor workspace: allowed."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=actor_id)
    _assert_can_write_entries(frozenset({"admin", "producer"}), actor_id, ws)


def test_write_entries_consumer_tenant_ws() -> None:
    """Consumer writing entries on a tenant workspace: denied."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant")
    with pytest.raises(WorkspaceOperationDenied):
        _assert_can_write_entries(frozenset({"consumer"}), actor_id, ws)


def test_write_entries_producer_other_actor_ws() -> None:
    """Producer writing entries on another actor's workspace: denied."""
    actor_id = uuid.uuid4()
    other = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=other)
    with pytest.raises(WorkspaceOperationDenied):
        _assert_can_write_entries(frozenset({"producer"}), actor_id, ws)


def test_write_entries_no_role_tenant_ws() -> None:
    """No-role actor writing entries on a tenant workspace: denied."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant")
    with pytest.raises(WorkspaceOperationDenied):
        _assert_can_write_entries(frozenset(), actor_id, ws)


# ---------------------------------------------------------------------------
# _assert_can_update_workspace — 12 cells (identical denial table)
# ---------------------------------------------------------------------------


def test_update_ws_producer_own_actor_ws_active() -> None:
    """Producer updating metadata on their own active actor workspace: allowed."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=actor_id)
    _assert_can_update_workspace(frozenset({"producer"}), actor_id, ws)


def test_update_ws_producer_own_actor_ws_archived() -> None:
    """Producer updating metadata on their own archived actor workspace: denied."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=actor_id, archived_at=_ARCHIVED)
    with pytest.raises(WorkspaceOperationDenied):
        _assert_can_update_workspace(frozenset({"producer"}), actor_id, ws)


def test_update_ws_producer_tenant_ws() -> None:
    """Producer updating a tenant workspace: denied."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant")
    with pytest.raises(WorkspaceOperationDenied):
        _assert_can_update_workspace(frozenset({"producer"}), actor_id, ws)


def test_update_ws_admin_tenant_ws_active() -> None:
    """Admin updating an active tenant workspace: allowed."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant")
    _assert_can_update_workspace(frozenset({"admin"}), actor_id, ws)


def test_update_ws_admin_tenant_ws_archived() -> None:
    """Admin updating an archived tenant workspace: denied."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant", archived_at=_ARCHIVED)
    with pytest.raises(WorkspaceOperationDenied):
        _assert_can_update_workspace(frozenset({"admin"}), actor_id, ws)


def test_update_ws_consumer_own_actor_ws() -> None:
    """Consumer updating their own actor workspace: denied."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=actor_id)
    with pytest.raises(WorkspaceOperationDenied):
        _assert_can_update_workspace(frozenset({"consumer"}), actor_id, ws)


def test_update_ws_auditor_actor_ws() -> None:
    """Auditor updating an actor workspace: denied."""
    actor_id = uuid.uuid4()
    other = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=other)
    with pytest.raises(WorkspaceOperationDenied):
        _assert_can_update_workspace(frozenset({"auditor"}), actor_id, ws)


def test_update_ws_auditor_tenant_ws() -> None:
    """Auditor updating a tenant workspace: denied."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant")
    with pytest.raises(WorkspaceOperationDenied):
        _assert_can_update_workspace(frozenset({"auditor"}), actor_id, ws)


def test_update_ws_admin_producer_own_actor_ws() -> None:
    """Admin+Producer updating their own actor workspace: allowed."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=actor_id)
    _assert_can_update_workspace(frozenset({"admin", "producer"}), actor_id, ws)


def test_update_ws_consumer_tenant_ws() -> None:
    """Consumer updating a tenant workspace: denied."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant")
    with pytest.raises(WorkspaceOperationDenied):
        _assert_can_update_workspace(frozenset({"consumer"}), actor_id, ws)


def test_update_ws_producer_other_actor_ws() -> None:
    """Producer updating another actor's workspace: denied."""
    actor_id = uuid.uuid4()
    other = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=other)
    with pytest.raises(WorkspaceOperationDenied):
        _assert_can_update_workspace(frozenset({"producer"}), actor_id, ws)


def test_update_ws_no_role_tenant_ws() -> None:
    """No-role actor updating a tenant workspace: denied."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant")
    with pytest.raises(WorkspaceOperationDenied):
        _assert_can_update_workspace(frozenset(), actor_id, ws)


# ---------------------------------------------------------------------------
# _assert_can_delete_workspace — 12 cells (archive-state-independent)
# ---------------------------------------------------------------------------


def test_delete_ws_producer_own_actor_ws_active() -> None:
    """Producer soft-deleting their own active actor workspace: allowed."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=actor_id)
    _assert_can_delete_workspace(frozenset({"producer"}), actor_id, ws)


def test_delete_ws_producer_own_actor_ws_archived() -> None:
    """Producer soft-deleting their own archived actor workspace: allowed (archive-state-independent)."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=actor_id, archived_at=_ARCHIVED)
    _assert_can_delete_workspace(frozenset({"producer"}), actor_id, ws)


def test_delete_ws_producer_tenant_ws() -> None:
    """Producer soft-deleting a tenant workspace: denied."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant")
    with pytest.raises(WorkspaceOperationDenied):
        _assert_can_delete_workspace(frozenset({"producer"}), actor_id, ws)


def test_delete_ws_admin_tenant_ws_active() -> None:
    """Admin soft-deleting an active tenant workspace: allowed."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant")
    _assert_can_delete_workspace(frozenset({"admin"}), actor_id, ws)


def test_delete_ws_admin_tenant_ws_archived() -> None:
    """Admin soft-deleting an archived tenant workspace: allowed (archive-state-independent)."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant", archived_at=_ARCHIVED)
    _assert_can_delete_workspace(frozenset({"admin"}), actor_id, ws)


def test_delete_ws_consumer_own_actor_ws() -> None:
    """Consumer soft-deleting their own actor workspace: denied."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=actor_id)
    with pytest.raises(WorkspaceOperationDenied):
        _assert_can_delete_workspace(frozenset({"consumer"}), actor_id, ws)


def test_delete_ws_auditor_actor_ws() -> None:
    """Auditor soft-deleting any actor workspace: denied."""
    actor_id = uuid.uuid4()
    other = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=other)
    with pytest.raises(WorkspaceOperationDenied):
        _assert_can_delete_workspace(frozenset({"auditor"}), actor_id, ws)


def test_delete_ws_auditor_tenant_ws() -> None:
    """Auditor soft-deleting a tenant workspace: denied."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant")
    with pytest.raises(WorkspaceOperationDenied):
        _assert_can_delete_workspace(frozenset({"auditor"}), actor_id, ws)


def test_delete_ws_admin_producer_own_actor_ws() -> None:
    """Admin+Producer soft-deleting their own actor workspace: allowed."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=actor_id)
    _assert_can_delete_workspace(frozenset({"admin", "producer"}), actor_id, ws)


def test_delete_ws_consumer_tenant_ws() -> None:
    """Consumer soft-deleting a tenant workspace: denied."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant")
    with pytest.raises(WorkspaceOperationDenied):
        _assert_can_delete_workspace(frozenset({"consumer"}), actor_id, ws)


def test_delete_ws_producer_other_actor_ws() -> None:
    """Producer soft-deleting another actor's workspace: denied."""
    actor_id = uuid.uuid4()
    other = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=other)
    with pytest.raises(WorkspaceOperationDenied):
        _assert_can_delete_workspace(frozenset({"producer"}), actor_id, ws)


def test_delete_ws_no_role_tenant_ws() -> None:
    """No-role actor soft-deleting a tenant workspace: denied."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant")
    with pytest.raises(WorkspaceOperationDenied):
        _assert_can_delete_workspace(frozenset(), actor_id, ws)


# ---------------------------------------------------------------------------
# _assert_can_archive_workspace — 10 cells (input state always unarchived)
# ---------------------------------------------------------------------------


def test_archive_ws_producer_own_actor_ws() -> None:
    """Producer archiving their own actor workspace: allowed."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=actor_id)
    _assert_can_archive_workspace(frozenset({"producer"}), actor_id, ws)


def test_archive_ws_producer_tenant_ws() -> None:
    """Producer archiving a tenant workspace: denied."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant")
    with pytest.raises(WorkspaceOperationDenied):
        _assert_can_archive_workspace(frozenset({"producer"}), actor_id, ws)


def test_archive_ws_admin_tenant_ws() -> None:
    """Admin archiving a tenant workspace: allowed."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant")
    _assert_can_archive_workspace(frozenset({"admin"}), actor_id, ws)


def test_archive_ws_consumer_own_actor_ws() -> None:
    """Consumer archiving their own actor workspace: denied."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=actor_id)
    with pytest.raises(WorkspaceOperationDenied):
        _assert_can_archive_workspace(frozenset({"consumer"}), actor_id, ws)


def test_archive_ws_auditor_actor_ws() -> None:
    """Auditor archiving an actor workspace: denied."""
    actor_id = uuid.uuid4()
    other = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=other)
    with pytest.raises(WorkspaceOperationDenied):
        _assert_can_archive_workspace(frozenset({"auditor"}), actor_id, ws)


def test_archive_ws_auditor_tenant_ws() -> None:
    """Auditor archiving a tenant workspace: denied."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant")
    with pytest.raises(WorkspaceOperationDenied):
        _assert_can_archive_workspace(frozenset({"auditor"}), actor_id, ws)


def test_archive_ws_admin_producer_own_actor_ws() -> None:
    """Admin+Producer archiving their own actor workspace: allowed."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=actor_id)
    _assert_can_archive_workspace(frozenset({"admin", "producer"}), actor_id, ws)


def test_archive_ws_consumer_tenant_ws() -> None:
    """Consumer archiving a tenant workspace: denied."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant")
    with pytest.raises(WorkspaceOperationDenied):
        _assert_can_archive_workspace(frozenset({"consumer"}), actor_id, ws)


def test_archive_ws_producer_other_actor_ws() -> None:
    """Producer archiving another actor's workspace: denied."""
    actor_id = uuid.uuid4()
    other = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=other)
    with pytest.raises(WorkspaceOperationDenied):
        _assert_can_archive_workspace(frozenset({"producer"}), actor_id, ws)


def test_archive_ws_no_role_tenant_ws() -> None:
    """No-role actor archiving a tenant workspace: denied."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant")
    with pytest.raises(WorkspaceOperationDenied):
        _assert_can_archive_workspace(frozenset(), actor_id, ws)


# ---------------------------------------------------------------------------
# Auditor write-attempt cases — 5 explicit cases (§5.1a)
# ---------------------------------------------------------------------------


def test_auditor_write_entries_actor_ws() -> None:
    """Auditor writing entries on an actor workspace: denied (not 404 — auditor perceives it)."""
    actor_id = uuid.uuid4()
    other = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=other)
    with pytest.raises(WorkspaceOperationDenied):
        _assert_can_write_entries(frozenset({"auditor"}), actor_id, ws)


def test_auditor_write_entries_tenant_ws() -> None:
    """Auditor writing entries on a tenant workspace: denied."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant")
    with pytest.raises(WorkspaceOperationDenied):
        _assert_can_write_entries(frozenset({"auditor"}), actor_id, ws)


def test_auditor_update_metadata_actor_ws() -> None:
    """Auditor updating metadata on an actor workspace: denied."""
    actor_id = uuid.uuid4()
    other = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=other)
    with pytest.raises(WorkspaceOperationDenied):
        _assert_can_update_workspace(frozenset({"auditor"}), actor_id, ws)


def test_auditor_archive_actor_ws() -> None:
    """Auditor archiving an actor workspace: denied."""
    actor_id = uuid.uuid4()
    other = uuid.uuid4()
    ws = _make_ws(owner_kind="actor", owner_actor_id=other)
    with pytest.raises(WorkspaceOperationDenied):
        _assert_can_archive_workspace(frozenset({"auditor"}), actor_id, ws)


def test_auditor_delete_tenant_ws() -> None:
    """Auditor soft-deleting a tenant workspace: denied."""
    actor_id = uuid.uuid4()
    ws = _make_ws(owner_kind="tenant")
    with pytest.raises(WorkspaceOperationDenied):
        _assert_can_delete_workspace(frozenset({"auditor"}), actor_id, ws)
