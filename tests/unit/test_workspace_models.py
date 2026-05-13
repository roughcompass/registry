"""Unit tests verifying the workspace ORM mapped class contracts.

All four workspace ORM classes must expose the exact column set agreed in the
WS-phase plaintext-only contract. The critical invariant: WorkspaceEntryRecord
must have ``body_md`` and must NOT have any ciphertext columns
(``body_ciphertext``, ``body_nonce``, ``references_ciphertext``,
``references_nonce``). Those columns belong to the ENC-phase ALTER TABLE and
must not appear on this class ahead of that migration.
"""

from __future__ import annotations

from registry.storage.models import (
    WorkspaceEntryRecord,
    WorkspaceRecord,
    WorkspaceShareAcceptanceRecord,
    WorkspaceShareRecord,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _column_names(model_cls: type) -> set[str]:
    """Return the set of column names declared on an ORM mapped class."""
    return {c.name for c in model_cls.__table__.columns}


# ---------------------------------------------------------------------------
# Table name contracts
# ---------------------------------------------------------------------------


def test_workspace_record_tablename() -> None:
    assert WorkspaceRecord.__tablename__ == "workspaces"


def test_workspace_entry_record_tablename() -> None:
    assert WorkspaceEntryRecord.__tablename__ == "workspace_entries"


def test_workspace_share_record_tablename() -> None:
    assert WorkspaceShareRecord.__tablename__ == "workspace_shares"


def test_workspace_share_acceptance_record_tablename() -> None:
    assert WorkspaceShareAcceptanceRecord.__tablename__ == "workspace_share_acceptances"


# ---------------------------------------------------------------------------
# WorkspaceEntryRecord — plaintext-only contract (critical)
# ---------------------------------------------------------------------------


def test_entry_has_body_md() -> None:
    """body_md must be present — it is NOT NULL in DDL and is the content column."""
    cols = _column_names(WorkspaceEntryRecord)
    assert "body_md" in cols, f"body_md missing from workspace_entries columns: {cols}"


def test_entry_has_no_ciphertext_columns() -> None:
    """ENC-phase columns must NOT exist on the WS-phase ORM class.

    body_ciphertext, body_nonce, references_ciphertext, references_nonce are
    added by the ENC-phase ALTER TABLE. Their presence here is a contract
    violation that would break service-layer assumptions about nullable/not-null
    semantics during the WS phase.
    """
    forbidden = {"body_ciphertext", "body_nonce", "references_ciphertext", "references_nonce"}
    cols = _column_names(WorkspaceEntryRecord)
    violations = forbidden & cols
    assert not violations, f"ENC-phase columns found on WorkspaceEntryRecord: {violations}"


def test_entry_has_references_jsonb() -> None:
    cols = _column_names(WorkspaceEntryRecord)
    assert "references_jsonb" in cols


def test_entry_has_reference_ids() -> None:
    cols = _column_names(WorkspaceEntryRecord)
    assert "reference_ids" in cols


# ---------------------------------------------------------------------------
# WorkspaceRecord — key column type checks
# ---------------------------------------------------------------------------


def test_workspace_encryption_tier_is_not_optional() -> None:
    """encryption_tier is NOT NULL with DEFAULT 'none' — Mapped[str], not Mapped[str | None]."""
    col = WorkspaceRecord.__table__.columns["encryption_tier"]
    assert not col.nullable, "encryption_tier must be NOT NULL"


def test_workspace_t_invalidated_at_is_optional() -> None:
    col = WorkspaceRecord.__table__.columns["t_invalidated_at"]
    assert col.nullable, "t_invalidated_at must be nullable (soft-delete sentinel)"


def test_workspace_archived_at_is_optional() -> None:
    col = WorkspaceRecord.__table__.columns["archived_at"]
    assert col.nullable, "archived_at must be nullable"


def test_workspace_has_owner_kind() -> None:
    cols = _column_names(WorkspaceRecord)
    assert "owner_kind" in cols


def test_workspace_has_owner_actor_id() -> None:
    cols = _column_names(WorkspaceRecord)
    assert "owner_actor_id" in cols


# ---------------------------------------------------------------------------
# WorkspaceShareRecord
# ---------------------------------------------------------------------------


def test_share_has_grantee_tenant_id() -> None:
    """grantee_tenant_id is needed for cross-tenant share detection."""
    cols = _column_names(WorkspaceShareRecord)
    assert "grantee_tenant_id" in cols


def test_share_revoked_at_is_optional() -> None:
    col = WorkspaceShareRecord.__table__.columns["revoked_at"]
    assert col.nullable, "revoked_at must be nullable (NULL = share is active)"


def test_share_role_has_default() -> None:
    col = WorkspaceShareRecord.__table__.columns["role"]
    assert col.default is not None or col.server_default is not None, (
        "role must carry a default value ('reader')"
    )


# ---------------------------------------------------------------------------
# WorkspaceShareAcceptanceRecord
# ---------------------------------------------------------------------------


def test_acceptance_has_accepting_tenant_id() -> None:
    cols = _column_names(WorkspaceShareAcceptanceRecord)
    assert "accepting_tenant_id" in cols


def test_acceptance_has_no_tenant_id_column() -> None:
    """workspace_share_acceptances has no tenant_id column in DDL — TenantMixin not applied."""
    cols = _column_names(WorkspaceShareAcceptanceRecord)
    assert "tenant_id" not in cols, (
        "workspace_share_acceptances DDL has no tenant_id; TenantMixin must not be applied"
    )


def test_acceptance_accepted_at_is_not_null() -> None:
    col = WorkspaceShareAcceptanceRecord.__table__.columns["accepted_at"]
    assert not col.nullable, "accepted_at must be NOT NULL"


# ---------------------------------------------------------------------------
# Import smoke test — all four classes resolve
# ---------------------------------------------------------------------------


def test_all_four_classes_import() -> None:
    """Verifies that all four ORM classes are importable and are distinct types."""
    classes = [
        WorkspaceRecord,
        WorkspaceEntryRecord,
        WorkspaceShareRecord,
        WorkspaceShareAcceptanceRecord,
    ]
    assert len({id(cls) for cls in classes}) == 4, "Expected four distinct ORM classes"
