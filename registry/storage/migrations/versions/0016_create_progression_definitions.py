"""Creates the progression_definitions table.

Revision ID: 0016_create_progression_definitions
Revises: 0015_add_tenant_external_id_and_provider
Create Date: 2026-05-12

Adds the bi-temporal `progression_definitions` table. Each row defines
the rules that govern how an entity of a given type transitions between
stages within a tenant. Rows are bi-temporal: `t_valid_from`/`t_valid_to`
track when a definition was authoritative in the real world;
`t_ingested_at`/`t_invalidated_at` track when the registry learned of
or retracted it.

`is_advisory` controls enforcement mode. When FALSE the service rejects
transitions that violate the definition; when TRUE it records a warning
and allows the transition.

The unique constraint on `(tenant_id, entity_type, t_valid_from)` ensures
no two definitions for the same entity type are valid from the same instant,
preventing ambiguity when the service resolves the current definition.

The active-set index on `(tenant_id, entity_type, t_valid_to)` supports the
common query pattern: fetch the current definition for a (tenant, entity_type)
pair filtered by `t_valid_to IS NULL OR t_valid_to > now()`.

upgrade:
  1. CREATE TABLE progression_definitions
  2. CREATE INDEX ix_progression_definitions_active

downgrade (reverse order):
  1. DROP INDEX ix_progression_definitions_active
  2. DROP TABLE progression_definitions
"""

from __future__ import annotations

from alembic import op

revision: str = "0016_create_progression_definitions"
down_revision: str | None = "0015_add_tenant_external_id_and_provider"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# ---------------------------------------------------------------------------
# SQL fragments
# ---------------------------------------------------------------------------

_CREATE_TABLE = """
CREATE TABLE progression_definitions (
    progression_id      UUID PRIMARY KEY,
    tenant_id           UUID NOT NULL REFERENCES tenants(tenant_id),
    entity_type         TEXT NOT NULL,
    definition          JSONB NOT NULL,
    is_advisory         BOOLEAN NOT NULL DEFAULT FALSE,
    t_valid_from        TIMESTAMPTZ NOT NULL,
    t_valid_to          TIMESTAMPTZ NULL,
    t_ingested_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    t_invalidated_at    TIMESTAMPTZ NULL,
    UNIQUE (tenant_id, entity_type, t_valid_from)
)
"""

_CREATE_INDEX = (
    "CREATE INDEX ix_progression_definitions_active "
    "ON progression_definitions (tenant_id, entity_type, t_valid_to)"
)

_DROP_INDEX = "DROP INDEX IF EXISTS ix_progression_definitions_active"

_DROP_TABLE = "DROP TABLE IF EXISTS progression_definitions"


# ---------------------------------------------------------------------------
# Migration body
# ---------------------------------------------------------------------------


def upgrade() -> None:
    op.execute(_CREATE_TABLE)
    op.execute(_CREATE_INDEX)


def downgrade() -> None:
    op.execute(_DROP_INDEX)
    op.execute(_DROP_TABLE)
