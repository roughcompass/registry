"""Drop api_tokens, roles, actor_roles; slim actors; add tenants.disabled_at.

Revision ID: 0021_entitlement_auth_consolidation
Revises: 0020_workspace_rbac
Create Date: 2026-05-14

Authentication and authorization moved from registry-issued opaque tokens
+ in-DB role grants to OIDC JWTs + an external entitlement service. The
catalog DB no longer stores api_tokens, roles, or actor_roles — those are
the entitlement service's concern. The Actor row stays as the audit-log
denormalization key, slimmed to enforce a unique (tenant_id, oidc_subject)
identity. The Tenant row gains a ``disabled_at`` operator override that
prevents JIT re-materialization.

Three tables dropped. Actor schema slimmed in-place (no row deletion;
preserves every audit_log.actor_id FK). disabled_at added to tenants
as nullable.

Steps:
1. Drop indexes on actor_roles (if_exists), then DROP TABLE actor_roles.
2. Drop indexes on api_tokens (if_exists), then DROP TABLE api_tokens.
3. Drop indexes on roles (if_exists), then DROP TABLE roles.
4. Drop the old idx_actors_oidc partial unique index (replaced by a
   plain UNIQUE constraint after oidc_subject becomes NOT NULL).
5. Backfill any NULL oidc_subject values with a stable ``__legacy__:``
   sentinel so the NOT NULL alter cannot fail and uniqueness is preserved.
6. ALTER actors.oidc_subject SET NOT NULL.
7. ALTER actors.display_name DROP NOT NULL.
8. ADD UNIQUE constraint uq_actors_tenant_oidc_subject (tenant_id,
   oidc_subject).
9. ADD COLUMN tenants.disabled_at TIMESTAMPTZ NULL.

Note on email + actor_kind columns: NOT dropped here. The sync-worker
subsystem in sync/runner.py uses Actor.actor_kind to distinguish humans
from sync workers. The auth ADR called for slimming these out, but the
sync path is independent of the auth rewrite — refactoring sync's actor
representation is its own follow-up. Both columns remain in the schema.

The downgrade reverses every change, recreating the dropped tables as
empty stubs and restoring the partial index. Downgrading after the
upgrade loses every actor_role / role / api_token row that existed
pre-upgrade — that data loss is intentional and irreversible. The
downgrade exists for Alembic version tracking and for non-prod rollback
of an immediate-prior upgrade against a fresh DB.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0021_entitlement_auth_consolidation"
down_revision: str | None = "0020_workspace_rbac"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


# ---------------------------------------------------------------------------
# Upgrade DDL
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # Step 1: drop indexes on actor_roles, then drop the table itself.
    # `if_exists=True` so a fresh DB without these indexes (already-migrated)
    # doesn't fail.
    op.execute("DROP INDEX IF EXISTS ix_actor_roles_actor_id")
    op.execute("DROP INDEX IF EXISTS ix_actor_roles_role_id")
    op.execute("DROP INDEX IF EXISTS ix_actor_roles_tenant_id")
    op.execute("DROP TABLE IF EXISTS actor_roles")

    # Step 2: drop api_tokens indexes + table.
    op.execute("DROP INDEX IF EXISTS ix_api_tokens_actor_id")
    op.execute("DROP INDEX IF EXISTS ix_api_tokens_tenant_id")
    op.execute("DROP INDEX IF EXISTS ix_api_tokens_token_hash")
    op.execute("DROP TABLE IF EXISTS api_tokens")

    # Step 3: drop roles indexes + table.
    op.execute("DROP INDEX IF EXISTS ix_roles_tenant_id")
    op.execute("DROP INDEX IF EXISTS uq_roles_tenant_name")
    op.execute("DROP TABLE IF EXISTS roles")

    # Step 4: drop the old partial unique index on actors. The new
    # plain UNIQUE constraint (added in step 8) replaces it.
    op.execute("DROP INDEX IF EXISTS idx_actors_oidc")

    # Step 5: backfill NULL oidc_subject so the NOT NULL alter cannot
    # fail and the upcoming UNIQUE constraint is well-defined. The
    # sentinel value is stable per-row so re-running the migration does
    # not change values.
    op.execute(
        "UPDATE actors "
        "SET oidc_subject = '__legacy__:' || actor_id::text "
        "WHERE oidc_subject IS NULL"
    )

    # Step 6: enforce NOT NULL on oidc_subject.
    op.alter_column("actors", "oidc_subject", existing_type=sa.Text(), nullable=False)

    # Step 7: relax NOT NULL on display_name (the entitlement-resolved
    # path uses claims.get("name") which may be absent).
    op.alter_column("actors", "display_name", existing_type=sa.Text(), nullable=True)

    # Step 8: replace the dropped partial index with a plain UNIQUE
    # constraint. (tenant_id, oidc_subject) was already effectively
    # unique among non-null oidc_subject rows; now it's unique always.
    op.create_unique_constraint(
        "uq_actors_tenant_oidc_subject", "actors", ["tenant_id", "oidc_subject"]
    )

    # Step 9: tenants gain the operator-override column.
    op.add_column(
        "tenants",
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
    )


# ---------------------------------------------------------------------------
# Downgrade DDL
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # Reverse step 9: drop disabled_at.
    op.drop_column("tenants", "disabled_at")

    # Reverse step 8: drop the plain unique constraint.
    op.drop_constraint("uq_actors_tenant_oidc_subject", "actors", type_="unique")

    # Reverse step 7: restore display_name NOT NULL. Backfill NULLs with
    # the oidc_subject value as a stable fallback (display_name was
    # nullable post-migration, so some rows may legitimately be NULL).
    op.execute(
        "UPDATE actors SET display_name = oidc_subject WHERE display_name IS NULL"
    )
    op.alter_column("actors", "display_name", existing_type=sa.Text(), nullable=False)

    # Reverse step 6: relax oidc_subject back to NULL-allowed.
    op.alter_column("actors", "oidc_subject", existing_type=sa.Text(), nullable=True)

    # Reverse steps 5 + 4: clear sentinel-prefixed oidc_subject values
    # (they were our backfill, not real data) and recreate the partial
    # unique index on (tenant_id, oidc_subject) WHERE oidc_subject IS
    # NOT NULL.
    op.execute("UPDATE actors SET oidc_subject = NULL WHERE oidc_subject LIKE '__legacy__:%'")
    op.execute(
        "CREATE UNIQUE INDEX idx_actors_oidc ON actors (tenant_id, oidc_subject) "
        "WHERE oidc_subject IS NOT NULL"
    )

    # Reverse step 3: recreate roles as an empty stub.
    op.execute(
        "CREATE TABLE roles ("
        " role_id UUID PRIMARY KEY,"
        " tenant_id UUID NOT NULL REFERENCES tenants(tenant_id),"
        " name TEXT NOT NULL,"
        " permissions TEXT[] NOT NULL DEFAULT '{}',"
        " created_at TIMESTAMPTZ NOT NULL"
        ")"
    )
    op.execute("CREATE INDEX ix_roles_tenant_id ON roles (tenant_id)")
    op.execute("CREATE UNIQUE INDEX uq_roles_tenant_name ON roles (tenant_id, name)")

    # Reverse step 2: recreate api_tokens as an empty stub.
    op.execute(
        "CREATE TABLE api_tokens ("
        " token_id UUID PRIMARY KEY,"
        " tenant_id UUID NOT NULL REFERENCES tenants(tenant_id),"
        " actor_id UUID NOT NULL REFERENCES actors(actor_id),"
        " token_hash TEXT UNIQUE NOT NULL,"
        " roles TEXT[] NOT NULL DEFAULT '{}',"
        " description TEXT,"
        " expires_at TIMESTAMPTZ,"
        " created_at TIMESTAMPTZ NOT NULL,"
        " revoked_at TIMESTAMPTZ"
        ")"
    )
    op.execute("CREATE INDEX ix_api_tokens_actor_id ON api_tokens (actor_id)")
    op.execute("CREATE INDEX ix_api_tokens_tenant_id ON api_tokens (tenant_id)")

    # Reverse step 1: recreate actor_roles as an empty stub.
    op.execute(
        "CREATE TABLE actor_roles ("
        " tenant_id UUID NOT NULL REFERENCES tenants(tenant_id),"
        " actor_id UUID NOT NULL REFERENCES actors(actor_id),"
        " role_id UUID NOT NULL REFERENCES roles(role_id),"
        " granted_at TIMESTAMPTZ NOT NULL,"
        " granted_by UUID REFERENCES actors(actor_id),"
        " PRIMARY KEY (tenant_id, actor_id, role_id)"
        ")"
    )
    op.execute("CREATE INDEX ix_actor_roles_actor_id ON actor_roles (actor_id)")
    op.execute("CREATE INDEX ix_actor_roles_role_id ON actor_roles (role_id)")
    op.execute("CREATE INDEX ix_actor_roles_tenant_id ON actor_roles (tenant_id)")
