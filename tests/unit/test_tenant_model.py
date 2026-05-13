"""Unit tests for the Tenant ORM model and the 0015 migration DDL.

Covers:
- ORM model field construction with explicit external_tenant_id + provider.
- ORM model defaults: provider defaults to 'manual', external_tenant_id is None.
- Migration DDL: upgrade() emits ADD COLUMN + CREATE UNIQUE INDEX statements.
- Migration DDL: upgrade() CHECK constraint allows only 'manual', 'jit', 'system'.
- Migration DDL: downgrade() drops index and both columns.
- Migration DDL: revision chain is correctly wired.

These tests run without a live database; constraint and index semantics are
verified by inspecting the SQL strings emitted by the patched op.execute.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

# ---------------------------------------------------------------------------
# Load the 0015 migration module without a DB connection
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent
_MIG_PATH = (
    _REPO_ROOT
    / "registry"
    / "storage"
    / "migrations"
    / "versions"
    / "0015_add_tenant_external_id_and_provider.py"
)

_MIG_SPEC = importlib.util.spec_from_file_location("migration_0015", _MIG_PATH)
assert _MIG_SPEC is not None and _MIG_SPEC.loader is not None
_mig = importlib.util.module_from_spec(_MIG_SPEC)
_MIG_SPEC.loader.exec_module(_mig)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _capture_upgrade() -> list[str]:
    """Run upgrade() with op.execute patched; return all SQL strings issued."""
    executed: list[str] = []

    def capture(sql: object) -> None:
        executed.append(str(sql))

    from alembic import op  # noqa: PLC0415

    original = getattr(op, "execute", None)
    try:
        op.execute = capture  # type: ignore[attr-defined]
        _mig.upgrade()
    finally:
        if original is not None:
            op.execute = original  # type: ignore[attr-defined]
    return executed


def _capture_downgrade() -> list[str]:
    """Run downgrade() with op.execute patched; return all SQL strings issued."""
    executed: list[str] = []

    def capture(sql: object) -> None:
        executed.append(str(sql))

    from alembic import op  # noqa: PLC0415

    original = getattr(op, "execute", None)
    try:
        op.execute = capture  # type: ignore[attr-defined]
        _mig.downgrade()
    finally:
        if original is not None:
            op.execute = original  # type: ignore[attr-defined]
    return executed


# ---------------------------------------------------------------------------
# ORM model field tests (pure Python, no DB required)
# ---------------------------------------------------------------------------


def test_tenant_explicit_external_id_and_provider() -> None:
    """Tenant accepts external_tenant_id and provider when supplied."""
    import datetime

    from registry.storage.models import Tenant

    t = Tenant(
        tenant_id=uuid.uuid4(),
        slug="seal-tenant",
        display_name="SEAL Tenant",
        created_at=datetime.datetime.now(tz=datetime.UTC),
        external_tenant_id="SEAL-112025",
        provider="jit",
    )
    assert t.external_tenant_id == "SEAL-112025"
    assert t.provider == "jit"


def test_tenant_defaults_provider_manual_and_external_id_none() -> None:
    """Tenant constructed without optional fields has None external_tenant_id.

    The provider default ('manual') is an INSERT-time default that fires during
    session flush, not at Python constructor time — so provider is None here.
    The ORM column's default value is verified via __table__ metadata instead.
    """
    import datetime

    from registry.storage.models import Tenant

    t = Tenant(
        tenant_id=uuid.uuid4(),
        slug="manual-tenant",
        display_name="Manual Tenant",
        created_at=datetime.datetime.now(tz=datetime.UTC),
    )
    assert t.external_tenant_id is None
    # Verify the INSERT-time default is registered on the column descriptor.
    provider_col = Tenant.__table__.c["provider"]  # type: ignore[attr-defined]
    assert provider_col.default is not None
    assert provider_col.default.arg == "manual"


def test_tenant_provider_column_exists_in_table_metadata() -> None:
    """The Tenant __table__ metadata exposes 'provider' and 'external_tenant_id' columns."""
    from registry.storage.models import Tenant

    col_names = {c.name for c in Tenant.__table__.c}  # type: ignore[attr-defined]
    assert "provider" in col_names
    assert "external_tenant_id" in col_names


# ---------------------------------------------------------------------------
# Migration revision chain
# ---------------------------------------------------------------------------


class TestMig0015Module:
    def test_revision_id(self) -> None:
        assert _mig.revision == "0015_add_tenant_external_id_and_provider"

    def test_down_revision(self) -> None:
        assert _mig.down_revision == "0014_visibility_public_rename"

    def test_upgrade_callable(self) -> None:
        assert callable(_mig.upgrade)

    def test_downgrade_callable(self) -> None:
        assert callable(_mig.downgrade)


# ---------------------------------------------------------------------------
# upgrade() DDL shape
# ---------------------------------------------------------------------------


class TestMig0015Upgrade:
    def test_external_tenant_id_column_added(self) -> None:
        combined = "\n".join(_capture_upgrade())
        assert "external_tenant_id" in combined
        assert "ADD COLUMN" in combined

    def test_external_tenant_id_is_nullable(self) -> None:
        """Column must be NULL-able — manually-provisioned tenants legitimately have no external ID."""
        stmts = _capture_upgrade()
        ext_id_stmts = [s for s in stmts if "external_tenant_id" in s and "ADD COLUMN" in s]
        assert len(ext_id_stmts) == 1
        # A NOT NULL constraint must not appear on this column's ADD COLUMN statement.
        assert "NOT NULL" not in ext_id_stmts[0]

    def test_provider_column_added_not_null_with_default(self) -> None:
        stmts = _capture_upgrade()
        provider_stmts = [s for s in stmts if "provider" in s and "ADD COLUMN" in s]
        assert len(provider_stmts) == 1
        assert "NOT NULL" in provider_stmts[0]
        assert "DEFAULT 'manual'" in provider_stmts[0]

    def test_provider_check_constraint_allows_generic_enum(self) -> None:
        """CHECK constraint must allow 'manual', 'jit', 'system' — not provider-specific labels."""
        stmts = _capture_upgrade()
        provider_stmts = [s for s in stmts if "provider" in s and "ADD COLUMN" in s]
        assert len(provider_stmts) == 1
        stmt = provider_stmts[0]
        for allowed in ("'manual'", "'jit'", "'system'"):
            assert allowed in stmt, f"CHECK constraint must include {allowed}"

    def test_provider_check_constraint_does_not_include_rsam(self) -> None:
        """The specific upstream source name must not appear in the schema enum."""
        combined = "\n".join(_capture_upgrade())
        assert "rsam" not in combined.lower()

    def test_partial_unique_index_created(self) -> None:
        combined = "\n".join(_capture_upgrade())
        assert "ix_tenants_external_tenant_id_provider" in combined
        assert "CREATE UNIQUE INDEX" in combined

    def test_partial_index_where_clause_excludes_nulls(self) -> None:
        """Partial index must have WHERE external_tenant_id IS NOT NULL so NULL rows are unconstrained."""
        stmts = _capture_upgrade()
        idx_stmts = [s for s in stmts if "ix_tenants_external_tenant_id_provider" in s]
        assert len(idx_stmts) == 1
        assert "WHERE external_tenant_id IS NOT NULL" in idx_stmts[0]


# ---------------------------------------------------------------------------
# downgrade() DDL shape
# ---------------------------------------------------------------------------


class TestMig0015Downgrade:
    def test_index_dropped(self) -> None:
        combined = "\n".join(_capture_downgrade())
        assert "ix_tenants_external_tenant_id_provider" in combined
        assert "DROP INDEX" in combined

    def test_provider_column_dropped(self) -> None:
        combined = "\n".join(_capture_downgrade())
        assert "provider" in combined
        assert "DROP COLUMN" in combined

    def test_external_tenant_id_column_dropped(self) -> None:
        combined = "\n".join(_capture_downgrade())
        assert "external_tenant_id" in combined
        assert "DROP COLUMN" in combined

    def test_index_dropped_before_columns(self) -> None:
        """Index must be removed before columns are dropped."""
        executed = _capture_downgrade()
        idx_pos = next((i for i, s in enumerate(executed) if "DROP INDEX" in s), None)
        col_pos = next((i for i, s in enumerate(executed) if "DROP COLUMN" in s), None)
        assert idx_pos is not None, "DROP INDEX statement not found"
        assert col_pos is not None, "DROP COLUMN statement not found"
        assert idx_pos < col_pos, "Index must be dropped before columns"
