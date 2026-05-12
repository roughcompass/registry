"""RBAC — roles, actor_roles, rate_limits; actors.oidc_subject index.

Revision ID: 0005_phase4_rbac_oidc
Revises: 0004_phase3_sync_infra
Create Date: 2026-05-07

Creates RBAC and OIDC infrastructure:

* ``roles``       — one row per named role per tenant
* ``actor_roles`` — many-to-many junction with grant metadata
* ``rate_limits`` — per-actor (or per-tenant default) rate-limit overrides

``actors.oidc_subject`` and the partial unique index
``idx_actors_oidc`` already exist from the baseline migration
(0001_phase0_baseline.py).  No ALTER TABLE is issued here to avoid a
duplicate-column error.  The upgrade() call includes a guard so a
re-entrant run is safe.

Default role *seeding* is intentionally absent from this migration.
Tenants don't exist at migration time; seeding is done by the service
layer when a tenant is created (``CatalogService.seed_default_roles``).

Statements are issued one-per-``op.execute`` (asyncpg single-statement
requirement).
"""

from __future__ import annotations

from alembic import op

revision = "0005_phase4_rbac_oidc"
down_revision: str | None = "0004_phase3_sync_infra"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


# ---------------------------------------------------------------------------
# DDL — roles
# ---------------------------------------------------------------------------

_ROLES_DDL = """
CREATE TABLE roles (
    role_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(tenant_id),
    name        TEXT NOT NULL,
    permissions TEXT[] NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_roles_name CHECK (name IN ('consumer','producer','admin','auditor'))
)
"""

_ROLES_TENANT_IDX = "CREATE INDEX idx_roles_tenant ON roles (tenant_id)"

_ROLES_TENANT_NAME_UNIQ = "CREATE UNIQUE INDEX uq_roles_tenant_name ON roles (tenant_id, name)"

# ---------------------------------------------------------------------------
# DDL — actor_roles
# ---------------------------------------------------------------------------

_ACTOR_ROLES_DDL = """
CREATE TABLE actor_roles (
    tenant_id  UUID NOT NULL REFERENCES tenants(tenant_id),
    actor_id   UUID NOT NULL REFERENCES actors(actor_id),
    role_id    UUID NOT NULL REFERENCES roles(role_id),
    granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    granted_by UUID REFERENCES actors(actor_id),
    PRIMARY KEY (tenant_id, actor_id, role_id)
)
"""

_ACTOR_ROLES_ACTOR_IDX = "CREATE INDEX idx_actor_roles_actor ON actor_roles (tenant_id, actor_id)"

# ---------------------------------------------------------------------------
# DDL — rate_limits
# ---------------------------------------------------------------------------

_RATE_LIMITS_DDL = """
CREATE TABLE rate_limits (
    limit_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID NOT NULL REFERENCES tenants(tenant_id),
    actor_id          UUID REFERENCES actors(actor_id),
    reads_per_second  INTEGER NOT NULL DEFAULT 100,
    writes_per_second INTEGER NOT NULL DEFAULT 10,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

# UNIQUE on (tenant_id, actor_id) handles the NULL actor_id case by using a
# partial unique index so that exactly one tenant-level default row is allowed
# (actor_id IS NULL) while still uniquely constraining per-actor rows.
_RATE_LIMITS_TENANT_DEFAULT_UNIQ = (
    "CREATE UNIQUE INDEX uq_rate_limits_tenant_default " "ON rate_limits (tenant_id) " "WHERE actor_id IS NULL"
)

_RATE_LIMITS_ACTOR_UNIQ = (
    "CREATE UNIQUE INDEX uq_rate_limits_actor " "ON rate_limits (tenant_id, actor_id) " "WHERE actor_id IS NOT NULL"
)

_RATE_LIMITS_TENANT_IDX = "CREATE INDEX idx_rate_limits_tenant ON rate_limits (tenant_id, actor_id)"

# ---------------------------------------------------------------------------
# Downgrade constants (reverse order)
# ---------------------------------------------------------------------------

_DROP_ACTOR_ROLES_ACTOR_IDX = "DROP INDEX IF EXISTS idx_actor_roles_actor"
_DROP_ACTOR_ROLES = "DROP TABLE IF EXISTS actor_roles CASCADE"

_DROP_RATE_LIMITS_TENANT_IDX = "DROP INDEX IF EXISTS idx_rate_limits_tenant"
_DROP_RATE_LIMITS_ACTOR_UNIQ = "DROP INDEX IF EXISTS uq_rate_limits_actor"
_DROP_RATE_LIMITS_TENANT_DEFAULT_UNIQ = "DROP INDEX IF EXISTS uq_rate_limits_tenant_default"
_DROP_RATE_LIMITS = "DROP TABLE IF EXISTS rate_limits CASCADE"

_DROP_ROLES_TENANT_NAME_UNIQ = "DROP INDEX IF EXISTS uq_roles_tenant_name"
_DROP_ROLES_TENANT_IDX = "DROP INDEX IF EXISTS idx_roles_tenant"
_DROP_ROLES = "DROP TABLE IF EXISTS roles CASCADE"


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # roles must precede actor_roles (FK target).
    op.execute(_ROLES_DDL)
    op.execute(_ROLES_TENANT_IDX)
    op.execute(_ROLES_TENANT_NAME_UNIQ)

    op.execute(_ACTOR_ROLES_DDL)
    op.execute(_ACTOR_ROLES_ACTOR_IDX)

    op.execute(_RATE_LIMITS_DDL)
    op.execute(_RATE_LIMITS_TENANT_DEFAULT_UNIQ)
    op.execute(_RATE_LIMITS_ACTOR_UNIQ)
    op.execute(_RATE_LIMITS_TENANT_IDX)

    # actors.oidc_subject column + idx_actors_oidc already exist from
    # 0001_phase0_baseline; nothing to add here.


def downgrade() -> None:
    op.execute(_DROP_ACTOR_ROLES_ACTOR_IDX)
    op.execute(_DROP_ACTOR_ROLES)

    op.execute(_DROP_RATE_LIMITS_TENANT_IDX)
    op.execute(_DROP_RATE_LIMITS_ACTOR_UNIQ)
    op.execute(_DROP_RATE_LIMITS_TENANT_DEFAULT_UNIQ)
    op.execute(_DROP_RATE_LIMITS)

    op.execute(_DROP_ROLES_TENANT_NAME_UNIQ)
    op.execute(_DROP_ROLES_TENANT_IDX)
    op.execute(_DROP_ROLES)
