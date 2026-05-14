"""Drop workspace_shares and workspace_share_acceptances; add owner_kind immutability trigger.

Revision ID: 0020_workspace_rbac
Revises: 0019_workspaces_plaintext
Create Date: 2026-05-13

The workspace share model (workspace_shares, workspace_share_acceptances) is
replaced by tenant-role-based access control. This migration:

1. Drops workspace_share_acceptances (has FK to workspace_shares).
2. Drops the uq_share partial index explicitly before the table drop.
3. Drops the cross-tenant INSERT trigger and its function from workspace_shares.
4. Drops workspace_shares (its idx_share_grantee is dropped implicitly).
5. Drops the owner_kind_change trigger and its function from workspaces.
   That trigger referenced workspace_shares in its body; it is broken after
   Step 4 and must be removed before it can fire on any UPDATE.
6. Adds a simpler owner_kind immutability trigger (trg_ws_owner_kind_immutable)
   that rejects any change to owner_kind with no cross-table dependency.

The downgrade recreates the pre-migration schema to satisfy Alembic version
tracking. Downgrading after a production migration loses all share row data —
that data loss is intentional and irreversible. The downgrade is only usable
against a pre-migration snapshot.
"""

from __future__ import annotations

from alembic import op

revision: str = "0020_workspace_rbac"
down_revision: str | None = "0019_workspaces_plaintext"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


# ---------------------------------------------------------------------------
# Upgrade DDL
# ---------------------------------------------------------------------------

# Step 1: Drop acceptance table first (FK to workspace_shares)
_DROP_ACCEPTANCES = "DROP TABLE workspace_share_acceptances"

# Step 2: Drop partial index explicitly before table drop (defensive)
_DROP_UQ_SHARE = "DROP INDEX IF EXISTS uq_share"

# Step 3: Drop cross-tenant trigger and its function
_DROP_TRIGGER_SHARE_CROSS_TENANT = "DROP TRIGGER IF EXISTS trg_ws_share_cross_tenant ON workspace_shares"
_DROP_FUNC_SHARE_CROSS_TENANT = "DROP FUNCTION IF EXISTS check_workspace_share_cross_tenant()"

# Step 4: Drop workspace_shares table (idx_share_grantee dropped implicitly)
_DROP_SHARES = "DROP TABLE workspace_shares"

# Step 5: Drop owner_kind_change trigger and its function.
# This trigger references workspace_shares in its body; it fires broken after
# Step 4 and must be removed before any UPDATE on workspaces can run.
_DROP_TRIGGER_OWNER_KIND_CHANGE = "DROP TRIGGER IF EXISTS trg_ws_owner_kind_change ON workspaces"
_DROP_FUNC_OWNER_KIND_CHANGE = "DROP FUNCTION IF EXISTS check_workspace_owner_kind_change()"

# Step 6: Add owner_kind immutability trigger (no cross-table dependency)
_CREATE_FUNC_OWNER_KIND_IMMUTABLE = """
CREATE OR REPLACE FUNCTION check_workspace_owner_kind_immutable()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.owner_kind IS DISTINCT FROM OLD.owner_kind THEN
        RAISE EXCEPTION 'owner_kind is immutable after creation';
    END IF;
    RETURN NEW;
END;
$$
"""

_CREATE_TRIGGER_OWNER_KIND_IMMUTABLE = """
CREATE TRIGGER trg_ws_owner_kind_immutable
BEFORE UPDATE ON workspaces
FOR EACH ROW EXECUTE FUNCTION check_workspace_owner_kind_immutable()
"""


# ---------------------------------------------------------------------------
# Downgrade DDL — recreates the 0019_workspaces_plaintext schema
# ---------------------------------------------------------------------------

# Downgrade Step 1: Drop the new immutability trigger and its function
_DOWN_DROP_TRIGGER_IMMUTABLE = "DROP TRIGGER IF EXISTS trg_ws_owner_kind_immutable ON workspaces"
_DOWN_DROP_FUNC_IMMUTABLE = "DROP FUNCTION IF EXISTS check_workspace_owner_kind_immutable()"

# Downgrade Step 2: Restore owner_kind_change trigger (verbatim from 0019)
_DOWN_CREATE_FUNC_OWNER_KIND_CHANGE = """
CREATE OR REPLACE FUNCTION check_workspace_owner_kind_change()
RETURNS TRIGGER LANGUAGE PLPGSQL AS $$
BEGIN
    IF NEW.owner_kind != OLD.owner_kind THEN
        IF EXISTS (
            SELECT 1 FROM workspace_shares
            WHERE workspace_id = NEW.workspace_id
              AND tenant_id != grantee_tenant_id
              AND revoked_at IS NULL
        ) THEN
            RAISE EXCEPTION
                'owner_kind change rejected: workspace % has active cross-tenant shares; '
                'revoke them before changing owner_kind',
                NEW.workspace_id;
        END IF;
    END IF;
    RETURN NEW;
END;
$$
"""

_DOWN_CREATE_TRIGGER_OWNER_KIND_CHANGE = """
CREATE TRIGGER trg_ws_owner_kind_change
BEFORE UPDATE OF owner_kind ON workspaces
FOR EACH ROW EXECUTE FUNCTION check_workspace_owner_kind_change()
"""

# Downgrade Step 3: Recreate workspace_shares table + indexes + cross-tenant trigger
_DOWN_CREATE_SHARES = """
CREATE TABLE workspace_shares (
    share_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id      UUID NOT NULL REFERENCES workspaces(workspace_id),
    tenant_id         UUID NOT NULL REFERENCES tenants(tenant_id),
    grantee_actor_id  UUID NOT NULL REFERENCES actors(actor_id),
    grantee_tenant_id UUID NOT NULL REFERENCES tenants(tenant_id),
    role              TEXT NOT NULL DEFAULT 'reader',
    granted_by        UUID REFERENCES actors(actor_id),
    granted_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at        TIMESTAMPTZ,
    CONSTRAINT chk_share_role CHECK (role IN ('reader','contributor'))
)
"""

_DOWN_CREATE_IDX_UQ_SHARE = (
    "CREATE UNIQUE INDEX uq_share ON workspace_shares (workspace_id, grantee_actor_id) " "WHERE revoked_at IS NULL"
)

_DOWN_CREATE_IDX_SHARE_GRANTEE = (
    "CREATE INDEX idx_share_grantee ON workspace_shares (grantee_actor_id) " "WHERE revoked_at IS NULL"
)

_DOWN_CREATE_FUNC_SHARE_CROSS_TENANT = """
CREATE OR REPLACE FUNCTION check_workspace_share_cross_tenant()
RETURNS TRIGGER LANGUAGE PLPGSQL AS $$
BEGIN
    IF (SELECT owner_kind FROM workspaces WHERE workspace_id = NEW.workspace_id) = 'actor'
       AND NEW.tenant_id != NEW.grantee_tenant_id THEN
        RAISE EXCEPTION
            'cross-tenant share rejected: workspace % is actor-owned; '
            'only tenant-owned workspaces may be shared cross-tenant',
            NEW.workspace_id;
    END IF;
    RETURN NEW;
END;
$$
"""

_DOWN_CREATE_TRIGGER_SHARE_CROSS_TENANT = """
CREATE TRIGGER trg_ws_share_cross_tenant
BEFORE INSERT ON workspace_shares
FOR EACH ROW EXECUTE FUNCTION check_workspace_share_cross_tenant()
"""

# Downgrade Step 4: Recreate workspace_share_acceptances table + unique index
_DOWN_CREATE_ACCEPTANCES = """
CREATE TABLE workspace_share_acceptances (
    acceptance_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    share_id            UUID NOT NULL REFERENCES workspace_shares(share_id),
    workspace_id        UUID NOT NULL REFERENCES workspaces(workspace_id),
    accepting_actor_id  UUID NOT NULL REFERENCES actors(actor_id),
    accepting_tenant_id UUID NOT NULL REFERENCES tenants(tenant_id),
    accepted_at         TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_DOWN_CREATE_IDX_UQ_ACCEPTANCE = (
    "CREATE UNIQUE INDEX uq_acceptance ON workspace_share_acceptances " "(share_id, accepting_actor_id)"
)


# ---------------------------------------------------------------------------
# Migration body
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # Step 1 — drop acceptance table first (FK to workspace_shares)
    op.execute(_DROP_ACCEPTANCES)

    # Step 2 — drop uq_share partial index explicitly (defensive; table drop
    # would remove it implicitly, but explicit ordering is clearer)
    op.execute(_DROP_UQ_SHARE)

    # Step 3 — drop cross-tenant trigger and function before table drop
    op.execute(_DROP_TRIGGER_SHARE_CROSS_TENANT)
    op.execute(_DROP_FUNC_SHARE_CROSS_TENANT)

    # Step 4 — drop workspace_shares (idx_share_grantee dropped implicitly)
    op.execute(_DROP_SHARES)

    # Step 5 — drop owner_kind_change trigger (references workspace_shares in
    # its body; broken after Step 4; must go before any workspaces UPDATE fires)
    op.execute(_DROP_TRIGGER_OWNER_KIND_CHANGE)
    op.execute(_DROP_FUNC_OWNER_KIND_CHANGE)

    # Step 6 — add simpler immutability trigger with no cross-table dependency
    op.execute(_CREATE_FUNC_OWNER_KIND_IMMUTABLE)
    op.execute(_CREATE_TRIGGER_OWNER_KIND_IMMUTABLE)


def downgrade() -> None:
    # Downgrade Step 1 — drop new immutability trigger and function
    op.execute(_DOWN_DROP_TRIGGER_IMMUTABLE)
    op.execute(_DOWN_DROP_FUNC_IMMUTABLE)

    # Downgrade Step 2 — restore owner_kind_change trigger from 0019
    op.execute(_DOWN_CREATE_FUNC_OWNER_KIND_CHANGE)
    op.execute(_DOWN_CREATE_TRIGGER_OWNER_KIND_CHANGE)

    # Downgrade Step 3 — recreate workspace_shares table + indexes + trigger
    op.execute(_DOWN_CREATE_SHARES)
    op.execute(_DOWN_CREATE_IDX_UQ_SHARE)
    op.execute(_DOWN_CREATE_IDX_SHARE_GRANTEE)
    op.execute(_DOWN_CREATE_FUNC_SHARE_CROSS_TENANT)
    op.execute(_DOWN_CREATE_TRIGGER_SHARE_CROSS_TENANT)

    # Downgrade Step 4 — recreate workspace_share_acceptances table + unique index
    op.execute(_DOWN_CREATE_ACCEPTANCES)
    op.execute(_DOWN_CREATE_IDX_UQ_ACCEPTANCE)
