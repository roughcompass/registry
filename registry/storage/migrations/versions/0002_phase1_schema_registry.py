"""Schema registry — adds capability_type_schemas.

Revision ID: 0002_phase1_schema_registry
Revises: 0001_phase0_baseline
Create Date: 2026-05-06

Adds the bi-temporal `capability_type_schemas` table and seeds the
`instance_of` edge_rel vocabulary value (used by CatalogService when a
capability is registered as an instance of a capability_type).
"""

from __future__ import annotations

from alembic import op

revision = "0002_phase1_schema_registry"
down_revision: str | None = "0001_phase0_baseline"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


_TABLE_DDL = """
CREATE TABLE capability_type_schemas (
    schema_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL REFERENCES tenants(tenant_id),
    type_name        TEXT NOT NULL,
    json_schema      JSONB NOT NULL,
    is_advisory      BOOLEAN NOT NULL DEFAULT TRUE,
    t_valid_from     TIMESTAMPTZ NOT NULL,
    t_valid_to       TIMESTAMPTZ,
    t_ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    t_invalidated_at TIMESTAMPTZ,
    created_by       UUID REFERENCES actors(actor_id)
)
"""

_INDEX_DDL = (
    "CREATE INDEX idx_captype_tenant_name ON capability_type_schemas (tenant_id, type_name) "
    "WHERE t_invalidated_at IS NULL"
)

DEFAULT_TENANT_UUID = "00000000-0000-0000-0000-000000000000"


def upgrade() -> None:
    op.execute(_TABLE_DDL)
    op.execute(_INDEX_DDL)
    # `instance_of` was included in the baseline seed; this is a defensive
    # idempotent insert in case a deployment landed without it (e.g. the
    # baseline seed list changed between revisions).
    op.execute(
        f"INSERT INTO vocabulary_values (tenant_id, kind, value, is_system) "
        f"VALUES ('{DEFAULT_TENANT_UUID}', 'edge_rel', 'instance_of', TRUE) "
        f"ON CONFLICT DO NOTHING"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS capability_type_schemas CASCADE")
