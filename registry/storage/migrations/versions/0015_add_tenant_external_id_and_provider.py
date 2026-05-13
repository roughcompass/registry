"""Adds external_tenant_id and provider columns to the tenants table.

Revision ID: 0015_add_tenant_external_id_and_provider
Revises: 0014_visibility_public_rename
Create Date: 2026-05-12

Adds two columns that support JIT tenant provisioning:

  external_tenant_id TEXT NULL
      The opaque ID assigned by an upstream identity system. Nullable because
      manually-provisioned tenants have no external counterpart. Multiple
      NULL rows are allowed (standard Postgres UNIQUE-with-NULLs behaviour);
      the partial unique index below enforces uniqueness only among non-NULL
      rows.

  provider TEXT NOT NULL DEFAULT 'manual'
      Discriminates how the tenant was created. Allowed values: 'manual',
      'jit', 'system'. The CHECK constraint is intentionally generic — the
      specific upstream source name belongs in audit-log payloads, not in
      the schema enum.

  Partial unique index ix_tenants_external_tenant_id_provider:
      ON tenants (external_tenant_id, provider) WHERE external_tenant_id IS NOT NULL
      Prevents two JIT-provisioned tenants from sharing the same external ID
      within the same provider, while allowing arbitrarily many manually-
      provisioned tenants (all with NULL external_tenant_id).

upgrade:
  1. ADD COLUMN external_tenant_id TEXT NULL
  2. ADD COLUMN provider TEXT NOT NULL DEFAULT 'manual'
         CHECK (provider IN ('manual', 'jit', 'system'))
  3. CREATE UNIQUE INDEX ix_tenants_external_tenant_id_provider
         ON tenants (external_tenant_id, provider) WHERE external_tenant_id IS NOT NULL

downgrade (reverse order):
  1. DROP INDEX ix_tenants_external_tenant_id_provider
  2. ALTER TABLE tenants DROP COLUMN provider
  3. ALTER TABLE tenants DROP COLUMN external_tenant_id
"""

from __future__ import annotations

from alembic import op

revision: str = "0015_add_tenant_external_id_and_provider"
down_revision: str | None = "0014_visibility_public_rename"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# ---------------------------------------------------------------------------
# SQL fragments
# ---------------------------------------------------------------------------

_ADD_EXTERNAL_TENANT_ID = (
    "ALTER TABLE tenants ADD COLUMN external_tenant_id TEXT NULL"
)

_ADD_PROVIDER = (
    "ALTER TABLE tenants ADD COLUMN provider TEXT NOT NULL DEFAULT 'manual' "
    "CHECK (provider IN ('manual', 'jit', 'system'))"
)

_CREATE_INDEX = (
    "CREATE UNIQUE INDEX ix_tenants_external_tenant_id_provider "
    "ON tenants (external_tenant_id, provider) "
    "WHERE external_tenant_id IS NOT NULL"
)

_DROP_INDEX = "DROP INDEX IF EXISTS ix_tenants_external_tenant_id_provider"

_DROP_PROVIDER = "ALTER TABLE tenants DROP COLUMN IF EXISTS provider"

_DROP_EXTERNAL_TENANT_ID = "ALTER TABLE tenants DROP COLUMN IF EXISTS external_tenant_id"

# The alembic_version table is created by Alembic on first use with a
# varchar(32) column. Revision IDs starting from this migration are longer
# than 32 characters, so we widen the column to TEXT before Alembic writes
# this revision's ID into it.
_WIDEN_VERSION_NUM = (
    "ALTER TABLE alembic_version ALTER COLUMN version_num TYPE TEXT"
)


# ---------------------------------------------------------------------------
# Migration body
# ---------------------------------------------------------------------------


def upgrade() -> None:
    op.execute(_WIDEN_VERSION_NUM)
    op.execute(_ADD_EXTERNAL_TENANT_ID)
    op.execute(_ADD_PROVIDER)
    op.execute(_CREATE_INDEX)


def downgrade() -> None:
    op.execute(_DROP_INDEX)
    op.execute(_DROP_PROVIDER)
    op.execute(_DROP_EXTERNAL_TENANT_ID)
