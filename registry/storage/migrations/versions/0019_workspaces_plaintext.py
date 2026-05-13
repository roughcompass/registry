"""Creates four workspace tables (plaintext-only, WS phase).

Revision ID: 0019_workspaces_plaintext
Revises: 0018_annotations_plaintext
Create Date: 2026-05-12

Creates workspaces, workspace_entries, workspace_shares, and
workspace_share_acceptances — all plaintext-only. No EncryptionService
dependency exists in this phase; ciphertext columns and XOR constraints are
deferred to the ENC phase, which adds them via ALTER TABLE.

Table decisions:
- workspaces.encryption_tier TEXT NOT NULL DEFAULT 'none' — forward-compat
  column so the is_regulated block and ENC-phase detection can read it in the
  WS phase. WS-phase code only reads it to enforce the regulated-tenant block.
- workspace_entries.body_md TEXT NOT NULL — ENC phase will DROP NOT NULL and
  add body_ciphertext/body_nonce. The FTS index on to_tsvector('english',
  body_md) silently excludes post-ENC NULL rows, so no index rebuild is needed.
- workspace_shares carries a BEFORE INSERT trigger (check_workspace_share_cross_tenant)
  as the DB-layer backstop for cross-tenant share enforcement. A service-layer
  guard returns HTTP 422 first; the trigger fires regardless of which code path
  caused the INSERT, guarding against direct-SQL bypasses of the service layer.
- workspaces carries a BEFORE UPDATE trigger (check_workspace_owner_kind_change)
  that rejects owner_kind changes while active cross-tenant shares exist. The
  choice is REJECT rather than silent revoke so admins are not surprised.

upgrade:
  1. CREATE TABLE workspaces + indexes + trigger function + trigger
  2. CREATE TABLE workspace_entries + indexes (including FTS GIN)
  3. CREATE TABLE workspace_shares + indexes + trigger function + trigger
  4. CREATE TABLE workspace_share_acceptances + unique index

downgrade (reverse dependency order):
  1. DROP TABLE workspace_share_acceptances
  2. DROP TABLE workspace_shares (drops its indexes and trigger)
  3. DROP TRIGGER trg_ws_share_cross_tenant (already dropped with table, but
     explicit for clarity; use IF EXISTS)
  4. DROP FUNCTION check_workspace_share_cross_tenant
  5. DROP TABLE workspace_entries
  6. DROP TRIGGER trg_ws_owner_kind_change (IF EXISTS)
  7. DROP FUNCTION check_workspace_owner_kind_change
  8. DROP TABLE workspaces (drops its indexes)
"""

from __future__ import annotations

from alembic import op

revision: str = "0019_workspaces_plaintext"
down_revision: str | None = "0018_annotations_plaintext"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# ---------------------------------------------------------------------------
# workspaces
# ---------------------------------------------------------------------------

_CREATE_WORKSPACES = """
CREATE TABLE workspaces (
    workspace_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL REFERENCES tenants(tenant_id),
    name             TEXT NOT NULL,
    description      TEXT,
    owner_kind       TEXT NOT NULL,
    owner_actor_id   UUID REFERENCES actors(actor_id),
    encryption_tier  TEXT NOT NULL DEFAULT 'none',
    archived_at      TIMESTAMPTZ,
    t_invalidated_at TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by       UUID REFERENCES actors(actor_id),
    CONSTRAINT chk_owner_kind CHECK (owner_kind IN ('actor','tenant')),
    CONSTRAINT chk_encryption_tier CHECK (encryption_tier IN (
        'none','paas_tenant_key','aws_kms','azure_key_vault','gcp_kms','hashicorp_vault'
    )),
    CONSTRAINT chk_actor_owner CHECK (
        (owner_kind = 'actor' AND owner_actor_id IS NOT NULL)
        OR owner_kind = 'tenant'
    )
)
"""

# Partial indexes exclude soft-deleted rows so the common read path (active
# workspaces only) is always covered by a small, focused index.
_CREATE_IDX_WS_TENANT = (
    "CREATE INDEX idx_ws_tenant ON workspaces (tenant_id) "
    "WHERE t_invalidated_at IS NULL"
)

_CREATE_IDX_WS_OWNER = (
    "CREATE INDEX idx_ws_owner ON workspaces (owner_actor_id) "
    "WHERE owner_actor_id IS NOT NULL"
)

# BEFORE UPDATE trigger: rejects owner_kind changes while active cross-tenant
# shares exist. The DB trigger is the backstop — a service-layer guard already
# validates this before the UPDATE, but the trigger fires regardless of which
# path causes the row change, guarding against direct-SQL bypasses.
_CREATE_FUNC_OWNER_KIND_CHANGE = """
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

_CREATE_TRIGGER_OWNER_KIND = """
CREATE TRIGGER trg_ws_owner_kind_change
BEFORE UPDATE OF owner_kind ON workspaces
FOR EACH ROW EXECUTE FUNCTION check_workspace_owner_kind_change()
"""

# ---------------------------------------------------------------------------
# workspace_entries
# ---------------------------------------------------------------------------

_CREATE_WORKSPACE_ENTRIES = """
CREATE TABLE workspace_entries (
    entry_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id     UUID NOT NULL REFERENCES workspaces(workspace_id),
    tenant_id        UUID NOT NULL REFERENCES tenants(tenant_id),
    kind             TEXT NOT NULL,
    body_md          TEXT NOT NULL,
    references_jsonb JSONB,
    reference_ids    UUID[] NOT NULL DEFAULT '{}',
    expires_at       TIMESTAMPTZ,
    t_invalidated_at TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by       UUID REFERENCES actors(actor_id),
    CONSTRAINT chk_entry_kind CHECK (
        kind IN ('note','decision','open_question','saved_query','saved_view','private_annotation')
    )
)
"""

_CREATE_IDX_WE_WORKSPACE = (
    "CREATE INDEX idx_we_workspace ON workspace_entries (workspace_id) "
    "WHERE t_invalidated_at IS NULL"
)

_CREATE_IDX_WE_TENANT = (
    "CREATE INDEX idx_we_tenant ON workspace_entries (tenant_id) "
    "WHERE t_invalidated_at IS NULL"
)

# GIN index on the UUID array column for efficient ANY(reference_ids) lookups.
_CREATE_IDX_WE_REFS = "CREATE INDEX idx_we_refs ON workspace_entries USING GIN (reference_ids)"

_CREATE_IDX_WE_EXPIRES = (
    "CREATE INDEX idx_we_expires ON workspace_entries (expires_at) "
    "WHERE expires_at IS NOT NULL AND t_invalidated_at IS NULL"
)

# Functional GIN index for full-text search over body_md. Functional indexes
# on to_tsvector exclude NULL input rows automatically, so the ENC-phase ALTER
# that drops NOT NULL from body_md does not break this index — post-ENC rows
# where body_md is NULL are simply absent from the index, which is correct
# (FTS over ciphertext is an ENC-phase concern).
_CREATE_IDX_WE_BODY_FTS = (
    "CREATE INDEX idx_we_body_fts ON workspace_entries "
    "USING GIN (to_tsvector('english', body_md)) "
    "WHERE t_invalidated_at IS NULL"
)

# ---------------------------------------------------------------------------
# workspace_shares
# ---------------------------------------------------------------------------

_CREATE_WORKSPACE_SHARES = """
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

# Unique partial index: one active share per (workspace, grantee). Revoking a
# share sets revoked_at which excludes the row from this index, allowing a new
# share grant to the same grantee after revocation.
_CREATE_IDX_UQ_SHARE = (
    "CREATE UNIQUE INDEX uq_share ON workspace_shares (workspace_id, grantee_actor_id) "
    "WHERE revoked_at IS NULL"
)

_CREATE_IDX_SHARE_GRANTEE = (
    "CREATE INDEX idx_share_grantee ON workspace_shares (grantee_actor_id) "
    "WHERE revoked_at IS NULL"
)

# BEFORE INSERT trigger: Layer 1 cross-tenant share enforcement. The trigger
# rejects INSERTs that would create a cross-tenant share on an actor-owned
# workspace. A service-layer guard (Layer 2) returns HTTP 422 before the DB
# INSERT is attempted; this trigger is the backstop that fires regardless of
# which code path caused the INSERT, defending against direct-SQL bypasses.
_CREATE_FUNC_SHARE_CROSS_TENANT = """
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

_CREATE_TRIGGER_SHARE_CROSS_TENANT = """
CREATE TRIGGER trg_ws_share_cross_tenant
BEFORE INSERT ON workspace_shares
FOR EACH ROW EXECUTE FUNCTION check_workspace_share_cross_tenant()
"""

# ---------------------------------------------------------------------------
# workspace_share_acceptances
# ---------------------------------------------------------------------------

_CREATE_WORKSPACE_SHARE_ACCEPTANCES = """
CREATE TABLE workspace_share_acceptances (
    acceptance_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    share_id            UUID NOT NULL REFERENCES workspace_shares(share_id),
    workspace_id        UUID NOT NULL REFERENCES workspaces(workspace_id),
    accepting_actor_id  UUID NOT NULL REFERENCES actors(actor_id),
    accepting_tenant_id UUID NOT NULL REFERENCES tenants(tenant_id),
    accepted_at         TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

# One acceptance record per (share, actor): idempotent on repeated cross-tenant
# first-access, and prevents double-billing of the acceptance audit trail.
_CREATE_IDX_UQ_ACCEPTANCE = (
    "CREATE UNIQUE INDEX uq_acceptance ON workspace_share_acceptances "
    "(share_id, accepting_actor_id)"
)

# ---------------------------------------------------------------------------
# Drop statements (downgrade)
# ---------------------------------------------------------------------------

_DROP_WORKSPACE_SHARE_ACCEPTANCES = "DROP TABLE IF EXISTS workspace_share_acceptances"
_DROP_WORKSPACE_SHARES = "DROP TABLE IF EXISTS workspace_shares"
_DROP_TRIGGER_SHARE_CROSS_TENANT = (
    "DROP TRIGGER IF EXISTS trg_ws_share_cross_tenant ON workspace_shares"
)
_DROP_FUNC_SHARE_CROSS_TENANT = "DROP FUNCTION IF EXISTS check_workspace_share_cross_tenant"
_DROP_WORKSPACE_ENTRIES = "DROP TABLE IF EXISTS workspace_entries"
_DROP_TRIGGER_OWNER_KIND = (
    "DROP TRIGGER IF EXISTS trg_ws_owner_kind_change ON workspaces"
)
_DROP_FUNC_OWNER_KIND_CHANGE = "DROP FUNCTION IF EXISTS check_workspace_owner_kind_change"
_DROP_WORKSPACES = "DROP TABLE IF EXISTS workspaces"


# ---------------------------------------------------------------------------
# Migration body
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # --- workspaces table + indexes + trigger ---
    op.execute(_CREATE_WORKSPACES)
    op.execute(_CREATE_IDX_WS_TENANT)
    op.execute(_CREATE_IDX_WS_OWNER)
    op.execute(_CREATE_FUNC_OWNER_KIND_CHANGE)
    op.execute(_CREATE_TRIGGER_OWNER_KIND)

    # --- workspace_entries table + all indexes (including FTS GIN) ---
    op.execute(_CREATE_WORKSPACE_ENTRIES)
    op.execute(_CREATE_IDX_WE_WORKSPACE)
    op.execute(_CREATE_IDX_WE_TENANT)
    op.execute(_CREATE_IDX_WE_REFS)
    op.execute(_CREATE_IDX_WE_EXPIRES)
    op.execute(_CREATE_IDX_WE_BODY_FTS)

    # --- workspace_shares table + indexes + trigger ---
    op.execute(_CREATE_WORKSPACE_SHARES)
    op.execute(_CREATE_IDX_UQ_SHARE)
    op.execute(_CREATE_IDX_SHARE_GRANTEE)
    op.execute(_CREATE_FUNC_SHARE_CROSS_TENANT)
    op.execute(_CREATE_TRIGGER_SHARE_CROSS_TENANT)

    # --- workspace_share_acceptances table + unique index ---
    op.execute(_CREATE_WORKSPACE_SHARE_ACCEPTANCES)
    op.execute(_CREATE_IDX_UQ_ACCEPTANCE)


def downgrade() -> None:
    # Drop in reverse dependency order.
    # workspace_share_acceptances has FKs to workspace_shares and workspaces.
    op.execute(_DROP_WORKSPACE_SHARE_ACCEPTANCES)

    # workspace_shares must go before triggers/functions that reference it, and
    # before workspaces (FK on workspace_id). Triggers are dropped implicitly
    # when the table is dropped, but we issue explicit DROP TRIGGER IF EXISTS
    # statements before the table drop for defensive clarity.
    op.execute(_DROP_TRIGGER_SHARE_CROSS_TENANT)
    op.execute(_DROP_WORKSPACE_SHARES)
    op.execute(_DROP_FUNC_SHARE_CROSS_TENANT)

    # workspace_entries has FK to workspaces.
    op.execute(_DROP_WORKSPACE_ENTRIES)

    # workspaces: drop the owner_kind trigger and function before the table.
    op.execute(_DROP_TRIGGER_OWNER_KIND)
    op.execute(_DROP_FUNC_OWNER_KIND_CHANGE)
    op.execute(_DROP_WORKSPACES)
