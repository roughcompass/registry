"""Creates the progression_overrides table.

Revision ID: 0017_create_progression_overrides
Revises: 0016_create_progression_definitions
Create Date: 2026-05-12

Adds the `progression_overrides` table, which records authorized single-use
grants that allow a specific entity to bypass a gate for a given
(from_state, to_state) transition within a validity window.

`bypass_skip_rules` (BOOLEAN NOT NULL DEFAULT FALSE) must be set explicitly
to TRUE when the override is intended to bypass skip-rule enforcement — the
default is conservative. Single-use semantics (each override can be consumed
at most once) are enforced at the service layer by checking `consumed_at IS
NULL` before consumption and writing `consumed_at` in the same transaction;
no DB constraint prevents a double-write, so the service must own this
invariant.

`gate_id` may be a specific gate identifier or "*" meaning "any gate".

The partial index `ix_progression_overrides_lookup` covers the common query:
find an unconsumed override for (entity_id, from_state, to_state). Filtering
on `consumed_at IS NULL` in the index predicate keeps the index small.

upgrade:
  1. CREATE TABLE progression_overrides
  2. CREATE INDEX ix_progression_overrides_lookup

downgrade (reverse order):
  1. DROP INDEX ix_progression_overrides_lookup
  2. DROP TABLE progression_overrides
"""

from __future__ import annotations

from alembic import op

revision: str = "0017_create_progression_overrides"
down_revision: str | None = "0016_create_progression_definitions"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# ---------------------------------------------------------------------------
# SQL fragments
# ---------------------------------------------------------------------------

_CREATE_TABLE = """
CREATE TABLE progression_overrides (
    override_id        UUID PRIMARY KEY,
    tenant_id          UUID NOT NULL REFERENCES tenants(tenant_id),
    entity_id          UUID NOT NULL REFERENCES entities(entity_id),
    from_state         TEXT NOT NULL,
    to_state           TEXT NOT NULL,
    gate_id            TEXT NOT NULL,
    bypass_skip_rules  BOOLEAN NOT NULL DEFAULT FALSE,
    reason             TEXT NOT NULL,
    authorized_by      UUID NOT NULL REFERENCES actors(actor_id),
    t_valid_from       TIMESTAMPTZ NOT NULL,
    t_valid_to         TIMESTAMPTZ NOT NULL,
    consumed_at        TIMESTAMPTZ NULL,
    audit_event_id     UUID NOT NULL
)
"""
# audit_event_id is stored but not declared as a FK because audit_log is a
# partitioned table and Postgres does not support FK references to the root
# of a partitioned table. Referential integrity is maintained at the service
# layer: the override creation handler inserts the audit row first, then uses
# its audit_id here.

_CREATE_INDEX = (
    "CREATE INDEX ix_progression_overrides_lookup "
    "ON progression_overrides (entity_id, from_state, to_state) "
    "WHERE consumed_at IS NULL"
)

_DROP_INDEX = "DROP INDEX IF EXISTS ix_progression_overrides_lookup"

_DROP_TABLE = "DROP TABLE IF EXISTS progression_overrides"


# ---------------------------------------------------------------------------
# Migration body
# ---------------------------------------------------------------------------


def upgrade() -> None:
    op.execute(_CREATE_TABLE)
    op.execute(_CREATE_INDEX)


def downgrade() -> None:
    op.execute(_DROP_INDEX)
    op.execute(_DROP_TABLE)
