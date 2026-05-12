"""Renames the visibility vocabulary value `public-in-fabric` to `public`.

Revision ID: 0014_visibility_public_rename
Revises: 0013_missing_indexes
Create Date: 2026-05-11

Drops the existing CHECK constraint on ``entities.visibility`` (named
``chk_entity_visibility``), backfills any rows that carry the old value,
and re-adds the constraint with the updated vocabulary.

upgrade:
  1. DROP CONSTRAINT chk_entity_visibility
  2. UPDATE entities SET visibility = 'public' WHERE visibility = 'public-in-fabric'
  3. ADD CONSTRAINT chk_entity_visibility CHECK (visibility IN ('private', 'tenant-shared', 'public'))

downgrade (reverse order):
  1. DROP CONSTRAINT chk_entity_visibility
  2. UPDATE entities SET visibility = 'public-in-fabric' WHERE visibility = 'public'
  3. ADD CONSTRAINT chk_entity_visibility CHECK (visibility IN ('private', 'tenant-shared', 'public-in-fabric'))

Statements are issued one-per-``op.execute`` (asyncpg single-statement requirement).
"""

from __future__ import annotations

from alembic import op

revision: str = "0014_visibility_public_rename"
down_revision: str | None = "0013_missing_indexes"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# ---------------------------------------------------------------------------
# SQL fragments
# ---------------------------------------------------------------------------

_DROP_CONSTRAINT = "ALTER TABLE entities DROP CONSTRAINT IF EXISTS chk_entity_visibility"

_BACKFILL_TO_PUBLIC = "UPDATE entities SET visibility = 'public' WHERE visibility = 'public-in-fabric'"

_ADD_CONSTRAINT_NEW = (
    "ALTER TABLE entities "
    "ADD CONSTRAINT chk_entity_visibility "
    "CHECK (visibility IN ('private', 'tenant-shared', 'public'))"
)

_BACKFILL_TO_LEGACY = "UPDATE entities SET visibility = 'public-in-fabric' WHERE visibility = 'public'"

_ADD_CONSTRAINT_LEGACY = (
    "ALTER TABLE entities "
    "ADD CONSTRAINT chk_entity_visibility "
    "CHECK (visibility IN ('private', 'tenant-shared', 'public-in-fabric'))"
)


# ---------------------------------------------------------------------------
# Migration body
# ---------------------------------------------------------------------------


def upgrade() -> None:
    op.execute(_DROP_CONSTRAINT)
    op.execute(_BACKFILL_TO_PUBLIC)
    op.execute(_ADD_CONSTRAINT_NEW)


def downgrade() -> None:
    op.execute(_DROP_CONSTRAINT)
    op.execute(_BACKFILL_TO_LEGACY)
    op.execute(_ADD_CONSTRAINT_LEGACY)
