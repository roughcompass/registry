# Operator runbook — workspaces (RBAC migration)

This runbook covers the operational steps for the workspace-rbac migration
that replaces per-workspace shares with tenant-role-based access control.

## What changed

Before this migration, workspace access was governed by per-workspace
`workspace_shares` rows. Each row granted one grantee actor a `reader` or
`contributor` role on one workspace.

After this migration, workspace access is governed by the tenant-wide
`actor_roles` assignments (consumer, producer, admin, auditor). The
`workspace_shares` and `workspace_share_acceptances` tables are dropped.
The cross-tenant share trigger and the unique share index are dropped.
A new `trg_ws_owner_kind_immutable` trigger enforces that `owner_kind`
cannot change after a workspace is created.

The migration is destructive: any data in `workspace_shares` or
`workspace_share_acceptances` is removed and has no replacement table.
Operators must capture pre-migration state before applying the migration
if any post-mortem reconstruction may be required.

## Deploy protocol

Maintenance-window cut-over. Rolling and blue-green deploys are not safe
because old and new instances disagree on authorization semantics.

1. Announce the maintenance window at least 7 days in advance. Include
   the share-to-role mapping table below so consumers can verify that
   every actor who held a share also holds an equivalent tenant role
   before the window opens.
2. Stop all application instances, background workers, and sync workers
   before the migration runs. The DROP TABLE on `workspace_shares`
   commits cannot be rolled back without a snapshot restore once the
   migration completes.
3. Apply Alembic revision `0020_workspace_rbac` against the production
   database.
4. Smoke-test against a known tenant before unsealing traffic:
   `GET /v1/workspaces` returns 200 for an admin actor, `GET
   /v1/workspaces/{id}` returns 200 for a tenant workspace visible to
   the actor, `POST /v1/workspaces/{id}/entries` returns 201 for a
   producer on their own workspace, and the same call returns 403 for
   an auditor (perceivable workspace, denied write).
5. Restore traffic.

If migration fails after the share-table DROP, the only recovery is to
restore the database from the pre-migration snapshot. The share tables
cannot be reconstructed from migration history.

## Pre-migration audit query

Run this audit query in production at least 24 hours before the
maintenance window. The output enumerates every active share so the
share-to-role mapping table below can be applied case by case.

```sql
-- Pre-migration audit: list every active share and the roles
-- the grantee actor currently holds in the workspace's tenant.
SELECT
    s.share_id,
    s.workspace_id,
    s.tenant_id        AS workspace_tenant_id,
    s.grantee_actor_id,
    s.grantee_tenant_id,
    s.role             AS share_role,
    array_agg(r.name)  AS grantee_current_roles
FROM workspace_shares s
LEFT JOIN actor_roles ar
    ON ar.actor_id  = s.grantee_actor_id
   AND ar.tenant_id = s.tenant_id
LEFT JOIN roles r
    ON r.role_id    = ar.role_id
WHERE s.revoked_at IS NULL
GROUP BY
    s.share_id,
    s.workspace_id,
    s.tenant_id,
    s.grantee_actor_id,
    s.grantee_tenant_id,
    s.role;
```

For every row where `grantee_current_roles` does not include an
appropriate role per the mapping below, the operator must assign the
matching tenant role to the grantee actor before the cut-over — or
accept the documented loss of access.

## Share-to-role mapping

The migration drops the share table outright; there is no automatic
role-assignment step. Operators apply this mapping manually through
whichever identity-provider or in-database tooling assigns
`actor_roles` rows.

| Pre-migration share role | Workspace `owner_kind` | Required post-migration role | Notes |
|---|---|---|---|
| `reader` | `tenant` | `consumer` | Tenant workspaces are readable by every consumer in the tenant. Assigning `consumer` preserves read access. |
| `reader` | `actor` | `consumer` (creator only) | Only the original creator retains read on a private workspace under the ownership carve-out. Non-creator readers lose access. |
| `contributor` | `tenant` | `admin` | Only admins can write tenant workspaces post-migration. Operators must decide whether to grant admin or accept loss of write capability. |
| `contributor` | `actor` | _(no analog — dropped)_ | Actor-owned workspaces are single-writer; non-owner contributors have no post-migration write path. |

Actors who appear in the audit query but do not receive a matching
post-migration role lose access at the cut-over. The migration does
not surface this loss; the operator's responsibility is to flag affected
actors before the window opens.

## Post-migration verification

After unsealing traffic, run:

```sql
-- Confirm the share tables are gone.
SELECT to_regclass('workspace_shares'), to_regclass('workspace_share_acceptances');
-- Both must return NULL.
```

And confirm the immutability trigger fires:

```sql
-- Attempt to mutate owner_kind on any workspace. The trigger must reject this.
UPDATE workspaces
SET owner_kind = CASE owner_kind WHEN 'actor' THEN 'tenant' ELSE 'actor' END
WHERE workspace_id = (SELECT workspace_id FROM workspaces LIMIT 1);
-- Expected: ERROR — owner_kind is immutable after creation.
```

## Rollback

The migration is one-way once `DROP TABLE workspace_shares` commits.
The Alembic `downgrade` reverses the schema, but the share rows are
not restored. Treat rollback as a full database restore from the
pre-migration snapshot. There is no in-place downgrade path that
preserves any post-cut-over writes.
