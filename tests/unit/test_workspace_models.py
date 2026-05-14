"""Unit tests verifying the workspace ORM mapped class contracts.

Two surviving classes carry the plaintext-only contract: WorkspaceRecord and
WorkspaceEntryRecord. The critical invariant — WorkspaceEntryRecord must
expose ``body_md`` and must not declare any ciphertext columns
(``body_ciphertext``, ``body_nonce``, ``references_ciphertext``,
``references_nonce``). Those columns belong to a future encryption migration
and must not appear on this class ahead of it.
"""

from __future__ import annotations

from registry.storage.models import (
    WorkspaceEntryRecord,
    WorkspaceRecord,
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


# ---------------------------------------------------------------------------
# WorkspaceEntryRecord — plaintext-only contract (critical)
# ---------------------------------------------------------------------------


def test_entry_has_body_md() -> None:
    """body_md must be present — it is NOT NULL in DDL and is the content column."""
    cols = _column_names(WorkspaceEntryRecord)
    assert "body_md" in cols, f"body_md missing from workspace_entries columns: {cols}"


def test_entry_has_no_ciphertext_columns() -> None:
    """No ciphertext columns may appear before the encryption migration ships."""
    cols = _column_names(WorkspaceEntryRecord)
    forbidden = {
        "body_ciphertext",
        "body_nonce",
        "references_ciphertext",
        "references_nonce",
    }
    overlap = cols & forbidden
    assert not overlap, (
        f"WorkspaceEntryRecord must not declare ciphertext columns yet; found: {overlap}"
    )


def test_entry_body_md_is_not_null() -> None:
    col = WorkspaceEntryRecord.__table__.columns["body_md"]
    assert not col.nullable, "body_md must be NOT NULL in the plaintext contract"


# ---------------------------------------------------------------------------
# WorkspaceRecord — ownership invariants
# ---------------------------------------------------------------------------


def test_workspace_has_owner_kind() -> None:
    cols = _column_names(WorkspaceRecord)
    assert "owner_kind" in cols


def test_workspace_has_owner_actor_id() -> None:
    cols = _column_names(WorkspaceRecord)
    assert "owner_actor_id" in cols


# ---------------------------------------------------------------------------
# Import smoke test — surviving classes resolve and are distinct types
# ---------------------------------------------------------------------------


def test_surviving_classes_import() -> None:
    """The two workspace ORM classes are importable and distinct."""
    classes = [WorkspaceRecord, WorkspaceEntryRecord]
    assert len(set(classes)) == 2, "WorkspaceRecord and WorkspaceEntryRecord must be distinct"
