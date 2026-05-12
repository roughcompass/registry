"""Unit tests — migration module importability and revision-chain structure.

Verifies that migration modules are importable without a live DB connection
and carry correct revision/down_revision metadata with callable upgrade/downgrade
hooks. This guards against accidental breakage of the Alembic revision chain.
"""

from __future__ import annotations

import importlib


def test_rbac_oidc_migration_importable() -> None:
    """0005_phase4_rbac_oidc module must be importable without a DB connection."""
    mod = importlib.import_module("catalog.storage.migrations.versions.0005_phase4_rbac_oidc")
    assert mod.revision == "0005_phase4_rbac_oidc"
    assert mod.down_revision == "0004_phase3_sync_infra"
    assert callable(mod.upgrade)
    assert callable(mod.downgrade)
